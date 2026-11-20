import logging
import re

import uvicorn

from agent import create_app


class RedactTemporaryLLMKeyFilter(logging.Filter):
    _PATTERN = re.compile(r"'temporary_llm_key': '[^']*'")

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
            if "temporary_llm_key" in msg:
                record.msg = self._PATTERN.sub("'temporary_llm_key': '<redacted>'", msg)
                record.args = ()
        except Exception:
            if isinstance(record.msg, str) and "temporary_llm_key" in record.msg:
                record.msg = self._PATTERN.sub("'temporary_llm_key': '<redacted>'", record.msg)
        return True


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s:%(name)s:%(message)s")
for _handler in logging.root.handlers:
    _handler.addFilter(RedactTemporaryLLMKeyFilter())
logger = logging.getLogger(__name__)

if __name__ == "__main__":
    logger.info("Starting USGS Water Agent server on http://0.0.0.0:9998")
    app = create_app()
    uvicorn.run(app, host="0.0.0.0", port=9998)
