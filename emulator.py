#!/usr/bin/env python3
"""Standalone PN532 NDEF Type 4 tag emulator for Android and iPhone.

Serves any NDEF message: URI records (--url), text records (--text),
MIME records from files (--mime), or a raw pre-encoded message
(--ndef-hex / --ndef-file). Fast serial reads, re-arm on explicit
release status only plus a starvation guard, Android-validated target
parameters (SAK 0x20, measured to activate both phones), and
offset-correct read answers. Broadcasts continuously until Ctrl+C.

Usage:
    python emulator.py --url https://example.com
    python emulator.py --text "Hello"
    python emulator.py --url https://example.com --text "Scan me"
    python emulator.py --mime image/png picture.png
    python emulator.py --ndef-hex D1010B5504...
"""
import argparse
import os
import time
from collections import namedtuple

import serial

GET_FIRMWARE = 0x02
SAM_CONFIG = 0x14
IN_RELEASE = 0x52
TG_INIT_AS_TARGET = 0x8C
TG_GET_DATA = 0x86
TG_SET_DATA = 0x8E

# Android-validated target parameters: mode 0x05 (PICC passive), SENS_RES
# 04 00, 3-byte UID 112233, SAK 0x20 (ISO-DEP, no MIFARE Classic bit).
# Newer Android stacks validate SENS_RES/SAK and stay silent on SAK 0x60
# (measured: SAK 0x20 config activated the OnePlus to the "no supported
# application" toast, the 0x60 config produced no reaction at all).
INIT_BYTES = bytes.fromhex(
    "0504001122332001FEA2A3A4A5A6A7C0C1C2C3C4C5C6C7FFFFAA9988776655443322110000"
)

# Floor for the advertised maximum NDEF size in the CC. The CC actually
# advertises the served message's size when that is larger, so any
# message that fits the 2-byte NDEF length field can be served.
NDEF_MAX_LENGTH = 0x0400

SW_OK = bytes([0x90, 0x00])
SW_NOT_FOUND = bytes([0x6A, 0x82])
SW_NOT_SUPPORTED = bytes([0x6A, 0x81])
SW_EOF = bytes([0x62, 0x82])

URI_PREFIXES = [
    (0x01, "http://www."),
    (0x02, "https://www."),
    (0x03, "http://"),
    (0x04, "https://"),
    (0x05, "tel:"),
    (0x06, "mailto:"),
]

TNF_WELL_KNOWN = 0x01
TNF_MEDIA = 0x02

# One NDEF record: type name format, type name, payload, optional id.
Record = namedtuple("Record", "tnf type payload id", defaults=[b""])


def encode_record(rec, first, last):
    """Encode one record with correct MB/ME/SR/IL flags and length form.

    A single short URI record must stay byte-identical to the layout both
    phones were verified against (header D1, type 55), so the flag
    arithmetic below must reproduce that for first == last == True.
    """
    flags = rec.tnf
    if first:
        flags |= 0x80
    if last:
        flags |= 0x40
    if len(rec.payload) <= 0xFF:
        flags |= 0x10
    if rec.id:
        flags |= 0x08
    head = [flags, len(rec.type)]
    if rec.id:
        head.append(len(rec.id))
    if len(rec.payload) <= 0xFF:
        head.append(len(rec.payload))
    else:
        # Long form is a 3-byte big-endian length: 0x00, then 2 bytes.
        head += len(rec.payload).to_bytes(3, "big")
    return bytes(head) + rec.type + rec.id + rec.payload


def encode_message(records):
    return b"".join(
        encode_record(r, i == 0, i == len(records) - 1)
        for i, r in enumerate(records)
    )


def uri_record(url):
    payload = None
    for code, scheme in URI_PREFIXES:
        if url.startswith(scheme):
            payload = bytes([code]) + url[len(scheme):].encode()
            break
    if payload is None:
        payload = bytes([0x00]) + url.encode()
    return Record(TNF_WELL_KNOWN, b"U", payload)


def text_record(text, lang="en"):
    # RTD text: status byte is the language-code length, then the
    # language code, then the text (UTF-8).
    return Record(TNF_WELL_KNOWN, b"T", bytes([len(lang)]) + lang.encode() + text.encode())


def mime_record(mime_type, data):
    return Record(TNF_MEDIA, mime_type.encode(), data)


FILE_NONE, FILE_CC, FILE_NDEF = None, "CC", "NDEF"


def trace(msg):
    now = time.time()
    stamp = time.strftime("%H:%M:%S", time.localtime(now)) + f".{int(now % 1 * 1000):03d}"
    print(f"[{stamp}] {msg}", flush=True)


class Pn532Sync:
    def __init__(self, port):
        # Short serial read timeout so read_response deadlines are real;
        # the default 0.2 s quantized every poll to ~200 ms steps.
        self.ser = serial.Serial(port=port, baudrate=115200, timeout=0.02)
        self.ser.dtr = False
        self.ser.rts = False
        self.ser.reset_input_buffer()
        self.ser.write(bytes([0x55] * 8))
        time.sleep(0.1)
        self.ser.reset_input_buffer()

    def close(self):
        self.ser.close()

    def send(self, cmd, data=b"", timeout=0.5):
        body = bytes([0xD4, cmd]) + bytes(data)
        length = len(body)
        frame = bytes([0x00, 0x00, 0xFF, length, (~length & 0xFF) + 1 & 0xFF]) \
            + body + bytes([(0x00 - (0xD4 + cmd + sum(data))) & 0xFF, 0x00])
        self.ser.write(frame)
        return self.read_response(cmd, timeout)

    def read_response(self, cmd, timeout):
        deadline = time.time() + timeout
        buf = b""
        while time.time() < deadline:
            chunk = self.ser.read(64)
            if chunk:
                buf += chunk
                resp, buf = self.extract(buf, cmd)
                if resp is not None:
                    return resp
        return None

    def extract(self, buf, cmd):
        while True:
            start = buf.find(b"\x00\x00\xFF")
            if start < 0 or len(buf) < start + 5:
                return None, buf
            length = buf[start + 3]
            lcs = buf[start + 4]
            if (length + lcs) & 0xFF != 0:
                buf = buf[start + 1:]
                continue
            end = start + 5 + length + 2
            if len(buf) < end:
                return None, buf
            if length == 0:  # ACK
                buf = buf[end:]
                continue
            payload = buf[start + 5:start + 5 + length]
            dcs = buf[start + 5 + length]
            if (0x00 - sum(payload)) & 0xFF != dcs or payload[0] != 0xD5:
                buf = buf[start + 1:]
                continue
            if payload[1] - 1 == cmd:
                return payload[2:], buf[end:]
            buf = buf[end:]
            continue
        return None, buf


def answer_apdu(apdu, ndef_file, cc, current_file):
    """Answer one reader APDU. Returns (response bytes, new current_file).

    Reads are sliced at the requested offset and length, unlike the
    ios.py answerer, which serves whole files. Chunked readers need this.
    Select-by-name is answered permissively because some stacks send
    SELECT parameters the strict NDEF AID check would reject (measured
    on an iPhone whose select matched at an unusual position).
    """
    ins, p1, p2 = apdu[1], apdu[2], apdu[3]
    offset = (p1 << 8) | p2
    lc = apdu[4]
    if ins == 0xA4 and p1 == 0x04:
        return SW_OK, current_file
    if ins == 0xA4 and p1 == 0x00:
        if apdu[5:7] == b"\xE1\x03":
            return SW_OK, FILE_CC
        if apdu[5:7] == b"\xE1\x04":
            return SW_OK, FILE_NDEF
        return SW_NOT_FOUND, current_file
    if ins == 0xB0:
        if current_file == FILE_NONE:
            return SW_NOT_FOUND, current_file
        data = cc if current_file == FILE_CC else ndef_file
        if offset >= len(data):
            return SW_EOF, current_file
        return bytes(data[offset:offset + lc]) + SW_OK, current_file
    return SW_NOT_SUPPORTED, current_file


def build_message(args):
    """Assemble the NDEF message from CLI inputs. Returns raw bytes."""
    if args.ndef_hex or args.ndef_file:
        if args.ndef_hex and args.ndef_file:
            raise SystemExit("--ndef-hex and --ndef-file are mutually exclusive")
        if args.url or args.text or args.mime:
            raise SystemExit("--ndef-hex/--ndef-file cannot be combined with other records")
        if args.ndef_hex:
            return bytes.fromhex(args.ndef_hex)
        with open(args.ndef_file, "rb") as f:
            return f.read()
    records = []
    for url in args.url or []:
        records.append(uri_record(url))
    for text in args.text or []:
        records.append(text_record(text))
    for mime_type, path in args.mime or []:
        with open(path, "rb") as f:
            records.append(mime_record(mime_type, f.read()))
    if not records:
        raise SystemExit("serve at least one record: --url, --text, --mime, --ndef-hex, or --ndef-file")
    return encode_message(records)


def serve_forever(pn, ndef, cc_deny):
    ndef_file = len(ndef).to_bytes(2, "big") + ndef
    advertised = max(len(ndef), NDEF_MAX_LENGTH)
    cc = bytearray(
        [0x00, 0x0F, 0x20, 0x00, 0x54, 0x00, 0xFF, 0x04, 0x06,
         0xE1, 0x04] + list(advertised.to_bytes(2, "big")) +
        [0x00, 0x00]
    )
    cc[14] = 0xFF if cc_deny else 0x00
    current_file = FILE_NONE
    quiet = 0
    taps = 0

    pn.send(TG_INIT_AS_TARGET, INIT_BYTES, timeout=0.3)
    trace("armed, waiting for a phone (Ctrl+C to stop)")
    while True:
        # TgGetData blocks on the chip until data or an error status; the
        # clone answers idle polls with 0x25 after ~200 ms, so the deadline
        # must exceed that to see status instead of a blind timeout.
        resp = pn.send(TG_GET_DATA, timeout=0.3)
        if resp is None or len(resp) == 0:
            # No answer yet: the target may still be armed, so do NOT
            # release on the first silent poll. But after a real session
            # the chip can go silent and never send 0x25/0x29 again, which
            # would leave the tag dead, so force a re-arm periodically.
            quiet += 1
            if quiet <= 3 or quiet % 50 == 0:
                trace(f"idle poll #{quiet}, no answer")
            if quiet % 10 == 0:
                trace("silent for 10 polls, forcing release and re-arm")
                pn.send(IN_RELEASE, b"\x00")
                r = pn.send(TG_INIT_AS_TARGET, INIT_BYTES, timeout=0.3)
                if r:
                    trace(f"RF field present, init response {r.hex().upper()}")
            continue
        if resp[0] in (0x29, 0x25):
            quiet += 1
            if quiet <= 3 or quiet % 50 == 0:
                trace(f"status {resp[0]:02X}, re-arm (#{quiet})")
            r = pn.send(TG_INIT_AS_TARGET, INIT_BYTES, timeout=0.3)
            # This clone answers TgInitAsTarget with a truncated 3-byte
            # response whenever a field powers the chip, so this means
            # "RF field present", not a completed activation.
            if r:
                trace(f"RF field present, init response {r.hex().upper()}")
            continue
        if quiet:
            taps += 1
            trace(f"tap #{taps}")
        quiet = 0
        apdu = resp[1:]
        if len(apdu) < 5:
            trace(f"short frame {resp.hex().upper()}")
            continue
        answer, current_file = answer_apdu(apdu, ndef_file, cc, current_file)
        trace(f"APDU {apdu.hex().upper()} -> {answer.hex().upper()}")
        r = pn.send(TG_SET_DATA, answer)
        if r is None or len(r) == 0 or r[0] != 0x00:
            trace(f"TgSetData status {r.hex().upper() if r else 'timeout'}, releasing")
            pn.send(IN_RELEASE, b"\x00")


def main():
    parser = argparse.ArgumentParser(description="Standalone PN532 NDEF Type 4 tag emulator")
    parser.add_argument("--url", action="append", metavar="URL",
                        help="URI record; repeatable")
    parser.add_argument("--text", action="append", metavar="TEXT",
                        help="well-known text record; repeatable")
    parser.add_argument("--mime", nargs=2, metavar=("TYPE", "FILE"), action="append",
                        help="MIME record from a file, e.g. --mime image/png pic.png; repeatable")
    parser.add_argument("--ndef-hex", help="raw NDEF message hex, served verbatim")
    parser.add_argument("--ndef-file", help="raw NDEF message file, served verbatim")
    parser.add_argument("--port", default=os.environ.get("PN532_PORT", "/dev/cu.usbserial-110"))
    parser.add_argument("--cc-writable", action="store_true", help="Advertise the tag as writable")
    args = parser.parse_args()

    ndef = build_message(args)
    if len(ndef) > 0xFFFF:
        raise SystemExit(f"NDEF message is {len(ndef)} bytes, the NDEF file limit is 65535")

    pn = Pn532Sync(args.port)
    fw = pn.send(GET_FIRMWARE, timeout=1.0)
    if fw is None:
        pn.close()
        raise SystemExit("no firmware response, device not reachable")
    preview = ndef[:24].hex().upper() + ("..." if len(ndef) > 24 else "")
    trace(f"firmware {fw.hex().upper()}, NDEF {len(ndef)} bytes: {preview}")
    pn.send(SAM_CONFIG, b"\x01", timeout=1.0)
    try:
        serve_forever(pn, ndef, cc_deny=not args.cc_writable)
    except KeyboardInterrupt:
        pass
    finally:
        pn.send(IN_RELEASE, b"\x00")
        pn.send(SAM_CONFIG, b"\x01", timeout=1.0)
        pn.close()


if __name__ == "__main__":
    main()
