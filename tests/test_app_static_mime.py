import mimetypes


def test_register_static_mime_types_restores_js_module_types():
    # The MIME-type forcing moved from app.py into src.app_helpers.setup_environment().
    from src.app_helpers import setup_environment

    original_js = mimetypes.types_map.get(".js")
    original_mjs = mimetypes.types_map.get(".mjs")
    try:
        mimetypes.types_map[".js"] = "text/plain"
        mimetypes.types_map.pop(".mjs", None)

        setup_environment()

        assert mimetypes.types_map[".js"] == "text/javascript"
        assert mimetypes.types_map[".mjs"] == "application/javascript"
    finally:
        if original_js is None:
            mimetypes.types_map.pop(".js", None)
        else:
            mimetypes.types_map[".js"] = original_js

        if original_mjs is None:
            mimetypes.types_map.pop(".mjs", None)
        else:
            mimetypes.types_map[".mjs"] = original_mjs
