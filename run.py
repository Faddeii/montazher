import os
import threading
import webbrowser

import uvicorn

from app.config import HOST, PORT

if __name__ == "__main__":
    url = f"http://{HOST}:{PORT}"
    print(f"Монтажёр запущен: {url}  (закройте окно, чтобы остановить)")
    if not os.getenv("NO_BROWSER"):
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    uvicorn.run("app.server:app", host=HOST, port=PORT, log_level="warning")
