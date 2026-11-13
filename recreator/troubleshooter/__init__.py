"""
Troubleshooter: takes over when the diagnosis plan ends without a fix that counts.

It tags the failure with one of five reasons, tries once more within strict
limits (one self-recovery pass, one access request, one Reviewer retry), then
hands the case to an engineer with a case file. It never loops.
See docs/troubleshooter.md.
"""
from .config import TroubleshootSettings
from .models import FailureReason, TroubleshootResult
from .runner import troubleshoot

__all__ = ["FailureReason", "TroubleshootResult", "TroubleshootSettings", "troubleshoot"]
