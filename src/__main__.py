import logging

import uvicorn

from agent import create_app

logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")

if __name__ == "__main__":
    print("[DEBUG] Starting USGS Water Agent server on http://0.0.0.0:9998")
    app = create_app()
    uvicorn.run(app, host="0.0.0.0", port=9998)
