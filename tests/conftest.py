from __future__ import annotations

import pytest
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance() or QApplication([])
    app.setQuitOnLastWindowClosed(False)
    yield app
    app.processEvents()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


@pytest.fixture(autouse=True)
def flush_qt_deferred_deletes():
    yield
    if QApplication.instance() is not None:
        # processEvents() alone does not deliver DeferredDelete without exec().
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
