from abc import ABC, abstractmethod
from enum import Enum
import logging
import os
import re
import sys
from threading import Event, Thread
import time
from typing import List, TextIO, cast

import click


# Used to split aaa\nbbbb\n to ('aaa', '\n', 'bbb', '\n')
NL_DELIMITER = re.compile('(\n)')
# Matches *common* ANSI control sequences
# https://en.wikipedia.org/wiki/ANSI_escape_code#CSI_sequences
CSI_SEQUENCE = re.compile('\033[0-9;]*[A-Za-z]')


class EraseableStream(object):
    """
    Wrapper around a terminal stream for writing output that can be
    erased.
    """

    def __init__(self, target):
        self.target = target
        self.lines_written = 0
        # We assume that the EraseableStream starts writing at column 0
        self.column = 0
        self.resize()

    def resize(self):
        self.size = os.get_terminal_size(self.target.fileno())

    def write(self, string):
        # We want to track how many lines we've written so that we can
        # back up and erase them. Tricky thing is handling wrapping.

        # Strip control sequences
        plain = CSI_SEQUENCE.sub('', string)

        for piece in NL_DELIMITER.split(plain):
            if piece == '\n':
                self.column = 0
                self.lines_written += 1
            else:
                self.column = self.column + len(piece)
                # self.column == self.size.column doesn't wrap -
                # normal modern terminals wrap when a character is written
                # that would be off-screen, not immediately when the
                # line is full.
                while self.column > self.size.columns:
                    self.column -= self.size.columns
                    self.lines_written += 1

        self.target.write(string)

    def erase(self):
        if self.column > 0:
            # move cursor to the beginning of line and delete whole line
            self.target.write("\033[0G\033[2K")
        for i in range(0, self.lines_written):
            # move up cursor and delete whole line
            self.target.write("\033[1A\033[2K")

        self.lines_written = 0
        self.column = 0
        self.target.flush()

    def flush(self):
        self.target.flush()


debug_log_format = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"


class ConsoleHandler(logging.Handler):
    def __init__(self, *args, stream=sys.stderr, **kwargs):
        self.stream = stream

        self.tty = self.stream.isatty()
        if self.tty:
            self.status_stream = EraseableStream(self.stream)
        else:
            self.status_stream = None

        self.debug_formatter = logging.Formatter(debug_log_format)
        self.info_formatter = logging.Formatter(
            click.style("%(levelname)s", bold=True, fg='blue') + " - %(message)s")
        self.error_formatter = logging.Formatter(
            click.style("%(levelname)s", bold=True, fg='red') + " - %(message)s")

        self.live_displays: List[LiveDisplay] = []

        super().__init__(*args, level=logging.INFO, **kwargs)

    def emit(self, record):
        self.acquire()
        try:
            if self.status_stream:
                self.status_stream.erase()

            if self.level < logging.INFO:
                formatted = self.debug_formatter.format(record)
            elif record.levelno == logging.INFO:
                formatted = self.info_formatter.format(record)
            else:
                formatted = self.error_formatter.format(record)

            print(formatted, file=self.stream)

            if self.status_stream:
                for live in self.live_displays:
                    live.render(cast(TextIO, self.status_stream), RenderWhen.RUNNING)
        finally:
            self.release()

    def update_live_displays(self):
        if self.status_stream:
            self.acquire()
            try:
                self.status_stream.erase()
                for i, live in enumerate(self.live_displays):
                    if i != 0:
                        self.status_stream.write("\n")
                    live.render(cast(TextIO, self.status_stream), RenderWhen.RUNNING)
            finally:
                self.release()

    @staticmethod
    def get_current():
        rootLogger = logging.getLogger()
        for h in rootLogger.handlers:
            if isinstance(h, ConsoleHandler):
                return h


class RenderWhen(Enum):
    RUNNING = 1
    DONE = 2
    INTERRUPTED = 3
    EXCEPTION = 4


class LiveDisplay(ABC):
    def __init__(self, update_frequency: float = 0):
        self.update_frequency = update_frequency

    def __enter__(self):
        self.handler = ConsoleHandler.get_current()
        if self.handler:
            self.handler.acquire()
            try:
                self.handler.stream.write("\033[?25l")
                self.handler.stream.flush()
                self.handler.live_displays.append(self)
                if self.update_frequency > 0:
                    self.thread = Thread(
                        target=self.run_update_thread, name=__class__.__name__ + ".update_thread"
                    )
                    self.thread_done = Event()
                    self.thread.start()
                else:
                    self.thread = None
                    self.thread_done = None
            finally:
                self.handler.release()
            self.handler.update_live_displays()

        return self

    def __exit__(self, exc_type, exc_value, traceback):
        if self.handler:
            self.handler.acquire()
            try:
                assert self.handler.live_displays.pop() == self
                last_display = not self.handler.live_displays
            finally:
                self.handler.release()

            if self.thread:
                assert self.thread_done
                self.thread_done.set()
                self.thread.join()
                self.thread_done = None
                self.thread = None

            stream = self.handler.stream

            self.handler.update_live_displays()
            stream.write("\033[?25h")
            stream.flush()
            self.handler = None

            if exc_type is None:
                when = RenderWhen.DONE
            elif exc_type == KeyboardInterrupt:
                when = RenderWhen.INTERRUPTED
            else:
                when = RenderWhen.EXCEPTION
            if last_display:
                self.render(stream, when)

    def run_update_thread(self):
        assert self.handler
        assert self.thread_done

        while not self.thread_done.wait(self.update_frequency):
            self.handler.update_live_displays()

    @abstractmethod
    def render(self, stream: TextIO, when: RenderWhen):
        pass

    def update(self):
        if self.handler:
            self.handler.update_live_displays()


class Status(LiveDisplay):
    def __init__(self, message):
        super().__init__(update_frequency=0.25)
        self.message = message

    def render(self, stream: TextIO, when: RenderWhen):
        if when == RenderWhen.RUNNING:
            pos = int(3 * (time.time() % 2))
            spinner_char = "⠇⠋⠙⠸⠴⠦"[pos]
            # spinner_char = "|/-\\"[pos]
            print(click.style(spinner_char, bold=True), self.message, file=stream, end="")
            stream.flush()
        elif when == RenderWhen.DONE:
            print(click.style("✓ ", fg="green", bold=True) + self.message,
                  "... done", file=stream)
        else:
            print(click.style("✗ ", fg="red", bold=True) + self.message,
                  f"... {'interupted' if when == RenderWhen.INTERRUPTED else ''}",
                  file=stream)


if __name__ == "__main__":
    handlers = [ConsoleHandler()]
    logging.basicConfig(level=logging.INFO, handlers=handlers)
    with Status("Thinking"):
        logging.info("Hmmm")
        time.sleep(1)
        with Status("Thinking deeper"):
            logging.info("Hmmm")
            time.sleep(1)
            logging.info("Hmmm")
            time.sleep(1)
    with Status("Thinking"):
        logging.info("Hmmm")
        time.sleep(1)
        with Status("Thinking deeper"):
            logging.info("Hmmm")
            time.sleep(1)
            logging.info("Hmmm")
            time.sleep(1)
