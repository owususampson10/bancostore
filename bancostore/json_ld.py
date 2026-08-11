import json

# Same escape set Django's own django.utils.html.json_script() uses --
# safe to embed inside a <script type="application/ld+json"> tag even if a
# field (product description, admin-entered social link name/URL, etc.)
# contains a literal "</script>", which json.dumps() alone leaves
# unescaped and would otherwise let that field's content break out of the
# script element and inject arbitrary HTML (CodeRabbit finding on PR #70).
_SCRIPT_TAG_ESCAPES = {ord(">"): "\\u003E", ord("<"): "\\u003C", ord("&"): "\\u0026"}


def dumps_for_script_tag(data):
    return json.dumps(data).translate(_SCRIPT_TAG_ESCAPES)
