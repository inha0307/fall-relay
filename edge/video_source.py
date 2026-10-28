"""Real-time video-file source for the production capture/inference pipeline."""

import math
import time


class VideoSource:
    def __init__(self, path, size, tail_s=2.5):
        import cv2

        self.cv2 = cv2
        self.cap = cv2.VideoCapture(str(path))
        self.fps = self.cap.get(cv2.CAP_PROP_FPS)
        if not self.cap.isOpened() or not math.isfinite(self.fps) or self.fps <= 0:
            self.cap.release()
            raise ValueError(f'cannot read video with valid FPS: {path}')
        self.size, self.tail_s = size, tail_s
        self.frames_read = 0

    def frames(self, stop):
        start = time.monotonic()
        origin = time.monotonic_ns()
        index, last, tail = 0, None, 0
        while not stop.is_set():
            ok, image = self.cap.read()
            if ok:
                last = self.cv2.resize(image, self.size)
                self.frames_read += 1
            elif last is None:
                raise ValueError("video contains no decodable frames")
            elif tail >= math.ceil(self.tail_s * self.fps):
                break
            else:
                tail += 1  # allow the final event's post-decision clip window to finish
            if stop.wait(max(0.0, start + index / self.fps - time.monotonic())):
                break
            yield index + 1, origin + round(index * 1e9 / self.fps), last.copy()
            index += 1

    def stop(self):
        self.cap.release()
