import importlib.util
import logging
from pathlib import Path


def _load_main_module():
    main_path = Path(__file__).resolve().parent.parent / "src" / "__main__.py"
    spec = importlib.util.spec_from_file_location("agent_main", main_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_redact_temporary_llm_key_record_formatting():
    """A LogRecord whose message contains the key → the formatted output has '<redacted>' and not the key."""
    main_mod = _load_main_module()
    filter_cls = main_mod.RedactTemporaryLLMKeyFilter

    record = logging.LogRecord(
        name="ichatbio.agent_executor",
        level=logging.INFO,
        pathname="agent_executor.py",
        lineno=42,
        msg="Accepting request: {'request_id': 'req-1', 'temporary_llm_key': 'icb-super-secret-key-12345'}",
        args=(),
        exc_info=None,
    )
    filt = filter_cls()
    filt.filter(record)

    formatter = logging.Formatter("%(levelname)s:%(name)s:%(message)s")
    output = formatter.format(record)

    assert "icb-super-secret-key-12345" not in output
    assert "'temporary_llm_key': '<redacted>'" in output
