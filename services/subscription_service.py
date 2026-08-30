"""Subscription lifecycle commands and access-state resolution."""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from time import perf_counter
from typing import Any

# Preserve the existing implementation while fixing the period-end cancellation invariant.
