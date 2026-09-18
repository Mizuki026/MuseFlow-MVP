from __future__ import annotations

import json

from museflow.api.app import create_app

print(json.dumps(create_app(demo_mode=True).openapi(), ensure_ascii=False, indent=2))
