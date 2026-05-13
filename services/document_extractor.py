"""
Zero-cost document text extraction.
Tries PDF text layer first, falls back to Tesseract OCR for scanned documents.
Vision API is never called from here — this module is intentionally AI-free.
"""
import base64
import io
import logging

_logger = logging.getLogger(__name__)

# Minimum characters extracted before we consider the result usable
_MIN_TEXT_LENGTH = 80


def extract_from_b64(b64_data, mimetype='', filename=''):
    """
    Extract text from a base64-encoded attachment.
    Returns (text, method) where method is one of:
      'pdf_text' | 'pdf_ocr' | 'image_ocr' | 'excel' | 'csv' | 'plain' | 'none'
    """
    try:
        raw = base64.b64decode(b64_data) if isinstance(b64_data, (str, bytes)) else b''
        return extract_text(raw, mimetype, filename)
    except Exception as e:
        _logger.debug('document_extractor.extract_from_b64 failed: %s', e)
        return '', 'none'


def extract_text(raw_bytes, mimetype='', filename=''):
    """
    Extract text from raw bytes.
    Returns (text, method).
    """
    mime = (mimetype or '').lower()
    name = (filename or '').lower()

    if 'pdf' in mime or name.endswith('.pdf'):
        return _extract_pdf(raw_bytes)

    if (any(x in mime for x in ('image/', 'jpeg', 'jpg', 'png', 'tiff', 'webp', 'gif', 'bmp'))
            or name.endswith(('.jpg', '.jpeg', '.png', '.tiff', '.tif', '.webp', '.bmp', '.gif'))):
        text = _ocr_image_bytes(raw_bytes)
        return (text, 'image_ocr') if len(text) >= _MIN_TEXT_LENGTH else ('', 'none')

    if any(x in mime for x in ('spreadsheet', 'excel', 'xlsx')) or name.endswith(('.xlsx', '.xls')):
        text = _extract_excel(raw_bytes)
        return (text, 'excel') if text else ('', 'none')

    if 'csv' in mime or name.endswith('.csv'):
        return raw_bytes.decode('utf-8', errors='ignore')[:6000], 'csv'

    if mime.startswith('text/') or name.endswith(('.txt', '.md')):
        return raw_bytes.decode('utf-8', errors='ignore')[:6000], 'plain'

    return '', 'none'


# ── PDF extraction ─────────────────────────────────────────────────────────


def _extract_pdf(raw_bytes):
    text = _pdf_text_layer(raw_bytes)
    if len(text) >= _MIN_TEXT_LENGTH:
        return text, 'pdf_text'
    # Scanned PDF — render pages and OCR
    ocr_text = _pdf_ocr(raw_bytes)
    if len(ocr_text) >= _MIN_TEXT_LENGTH:
        return ocr_text, 'pdf_ocr'
    return '', 'none'


def _pdf_text_layer(raw_bytes):
    try:
        import pdfplumber
        with pdfplumber.open(io.BytesIO(raw_bytes)) as pdf:
            pages = []
            for page in pdf.pages[:10]:
                t = page.extract_text() or ''
                pages.append(t)
            return '\n'.join(pages).strip()
    except Exception as e:
        _logger.debug('pdfplumber text extraction failed: %s', e)
        return ''


def _pdf_ocr(raw_bytes):
    """Render PDF pages as images and run Tesseract OCR on each."""
    try:
        from pdf2image import convert_from_bytes
        images = convert_from_bytes(raw_bytes, dpi=200, first_page=1, last_page=6)
        texts = [_ocr_pil_image(img) for img in images]
        return '\n\n'.join(t for t in texts if t).strip()
    except ImportError:
        _logger.debug('pdf2image not installed — skipping PDF OCR')
    except Exception as e:
        _logger.debug('PDF OCR failed: %s', e)
    return ''


# ── Image OCR ──────────────────────────────────────────────────────────────


def _ocr_image_bytes(raw_bytes):
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(raw_bytes))
        return _ocr_pil_image(img)
    except Exception as e:
        _logger.debug('Image OCR failed: %s', e)
    return ''


def _ocr_pil_image(pil_image):
    try:
        import pytesseract
        text = pytesseract.image_to_string(pil_image, config='--psm 6')
        return text.strip()
    except Exception as e:
        _logger.debug('pytesseract failed: %s', e)
    return ''


# ── Excel ──────────────────────────────────────────────────────────────────


def _extract_excel(raw_bytes):
    try:
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(raw_bytes), read_only=True, data_only=True)
        rows = []
        for ws in wb.worksheets[:3]:
            for row in ws.iter_rows(max_row=100, values_only=True):
                line = '\t'.join(str(c) if c is not None else '' for c in row)
                if line.strip():
                    rows.append(line)
        return '\n'.join(rows)
    except Exception as e:
        _logger.debug('Excel extraction failed: %s', e)
        return ''
