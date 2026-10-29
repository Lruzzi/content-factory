"""Errors raised by the render step."""


class RenderError(RuntimeError):
    """FFmpeg or ffprobe could not do it. The message ends with FFmpeg's own explanation."""


class AssetError(RenderError):
    """A logo or other asset the profile asks for is missing. Fix the file or switch the overlay off."""
