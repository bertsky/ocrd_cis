import json
from ocrd_utils import resource_string


def get_ocrd_tool():
    return json.loads(resource_string(__name__, 'ocrd-tool.json'))
