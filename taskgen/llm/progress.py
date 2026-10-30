"""Live status line for a streaming model call, with a wall-clock deadline."""
import time


class Progress:
    """One live status line per streaming call: elapsed, tokens streamed so far (thinking vs answer), deadline."""

    def __init__(self, label, deadline_s):
        self.label, self.deadline_s, self.start = label, deadline_s, time.monotonic()
        self.chars = {'thinking': 0, 'answer': 0}
        self.last = 0.0

    def event(self, ev):
        if ev.type == 'content_block_delta':
            d = ev.delta
            self.add('thinking' if d.type == 'thinking_delta' else 'answer',
                     getattr(d, 'thinking', None) or getattr(d, 'text', None) or getattr(d, 'partial_json', None) or '')
        self.tick()

    def add(self, kind, text):
        self.chars[kind] += len(text or '')

    def tick(self):
        elapsed = time.monotonic() - self.start
        if self.deadline_s and elapsed > self.deadline_s:
            raise TimeoutError(f'{self.label}: no complete answer after {self.deadline_s}s '
                               f'(thinking ~{self.chars["thinking"] // 4} tok, answer ~{self.chars["answer"] // 4} tok)')
        if elapsed - self.last >= 5:
            self.last = elapsed
            print(f'\r    {self.label}: {elapsed:4.0f}s | thinking ~{self.chars["thinking"] // 4} tok | '
                  f'answer ~{self.chars["answer"] // 4} tok | limit {self.deadline_s}s   ', end='', flush=True)

    def done(self):
        print(f'\r    {self.label}: done in {time.monotonic() - self.start:.0f}s | thinking ~{self.chars["thinking"] // 4} '
              f'tok | answer ~{self.chars["answer"] // 4} tok' + ' ' * 20, flush=True)
