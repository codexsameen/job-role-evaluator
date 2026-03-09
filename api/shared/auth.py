import base64
import json

def get_user_id(req):
    header = req.headers.get("x-ms-client-principal")
    if not header:
        return None
    try:
        decoded = base64.b64decode(header).decode("utf-8")
        return json.loads(decoded).get("userId")
    except Exception:
        return None
