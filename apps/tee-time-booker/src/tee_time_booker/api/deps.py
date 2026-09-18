"""What a route can ask for: the session factory, the settings, the worker.

All three are created once in the app's lifespan and hung on `app.state`,
so a test can build the app around its own database and a worker it can
wait on.
"""

from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.orm import Session, sessionmaker

from tee_time_booker.config import Settings
from tee_time_booker.worker import BookingWorker


def _sessions(request: Request) -> sessionmaker[Session]:
    return request.app.state.sessions


def _settings(request: Request) -> Settings:
    return request.app.state.settings


def _worker(request: Request) -> BookingWorker:
    return request.app.state.worker


Sessions = Annotated[sessionmaker[Session], Depends(_sessions)]
AppSettings = Annotated[Settings, Depends(_settings)]
Worker = Annotated[BookingWorker, Depends(_worker)]
