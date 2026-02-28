from odoo import models


class PerformanceSampler(models.AbstractModel):
    _name = "prema.ai.performance.sampler"
    _description = "Prema AI Performance Sampler"

    def sample(self):
        return self.env["prema.ai.performance"].analyze()
