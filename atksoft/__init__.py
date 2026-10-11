"""ATKsoft -- an open control panel for Attack Shark mice.

Three layers, and nothing above them:

``atksoft.protocol``
    the wire format: one dataclass per vendor feature report, with ``build()``
    (host -> device) and ``from_bytes()`` (device -> host), plus the 0xA0 read
    handshake and the interrupt-IN event decoder.  Pure data, no I/O.

``atksoft.hidraw``
    Linux hidraw plumbing (``HIDIOCGFEATURE`` / ``HIDIOCSFEATURE`` ioctls) and
    the report-descriptor walk that says how long each report is.

``atksoft.session``
    :class:`MouseSession`: find the configuration interface, read one report,
    write one back.  This is the layer the CLI and the GUI both sit on.
"""

__version__ = "0.1.0"
