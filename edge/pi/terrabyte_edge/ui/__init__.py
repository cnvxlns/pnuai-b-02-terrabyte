"""On-screen display for the gateway.

Split deliberately: ``render`` decides what to show and is pure, ``dashboard``
maps that onto Tk widgets. Only the latter imports tkinter, so the display can
be tested on a machine with no X server.
"""
