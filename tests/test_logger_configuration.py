from src.core.logger import logger


def test_rag_logger_does_not_propagate_to_root_logger():
    assert logger.propagate is False
