from gatherlink.shared.logging import get_logger


def test_package_imports() -> None:
    logger = get_logger("gatherlink.test")
    assert logger.name == "gatherlink.test"
