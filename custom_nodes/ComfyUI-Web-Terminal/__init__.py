import threading
import time

WEB_DIRECTORY = "./js"
NODE_CLASS_MAPPINGS = {}
NODE_DISPLAY_NAME_MAPPINGS = {}
__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS", "WEB_DIRECTORY"]

try:
    from . import terminal_server
    import server

    _registered = False
    _lock = threading.Lock()

    def _try_register() -> bool:
        global _registered
        with _lock:
            if _registered:
                return True
            prompt_instance = getattr(server.PromptServer, "instance", None)
            if prompt_instance is None:
                return False
            app = getattr(prompt_instance, "app", None)
            if app is None:
                return False
            terminal_server.setup(app)
            _registered = True
            print("[ComfyUI-Web-Terminal] Interactive PTY terminal routes registered (/ws/terminal, /terminal).")
            return True

    def _register_with_retry():
        for _ in range(120):
            try:
                if _try_register():
                    return
            except Exception as err:
                print(f"[ComfyUI-Web-Terminal] Route registration attempt failed: {err}")
            time.sleep(0.5)
        print("[ComfyUI-Web-Terminal] Timed out waiting for PromptServer.instance.")

    if not _try_register():
        threading.Thread(target=_register_with_retry, daemon=True).start()
except Exception as exc:
    print(f"[ComfyUI-Web-Terminal] Failed to initialize: {exc}")
