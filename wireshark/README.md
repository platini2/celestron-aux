# Celestron AUX dissector for Wireshark's source tree

`packet-celestron-aux.c` is a built-in Wireshark dissector for the Celestron AUX
bus, written to be contributed to Wireshark itself (`epan/dissectors`). It does
what the Lua plugin from `celestron-aux --export-dissector` does, without
installing anything: device and command names, every payload decoded the way
celestron-aux decodes it, checksums, and TCP reassembly.

Unlike the Lua plugin, it carries only the built-in names (not your own catalog),
and it does not claim TCP port 2000 by default, because Wireshark gives that port
to Cisco's Skinny protocol. To decode a Wi-Fi capture, either:

- **Decode As**: right-click a packet on port 2000, **Decode As...**, and choose
  `CELESTRON_AUX` (in tshark: `-d tcp.port==2000,celestron_aux`); or
- turn on the heuristic: **Analyze > Enabled Protocols**, search for
  "Celestron", tick `celestron_aux_tcp` (in tshark:
  `--enable-heuristic celestron_aux_tcp`). It recognises a TCP stream that
  starts with a complete AUX packet with a valid checksum, on any port.

For Wi-Fi captures, also turn on **Preferences > Protocols > TCP > Reassemble
out-of-order segments** (`-o tcp.reassemble_out_of_order:TRUE`): retransmitted
segments often carry new bytes too, and without it their frames are not shown.

pcap files from `celestron-aux --export-pcap` use link type USER0: map it under
**Preferences > Protocols > DLT_USER** (DLT 147, payload protocol `celestron_aux`).

The licence of this file is GPL-2.0-or-later, as Wireshark requires; the rest of
celestron-aux stays GPL-3.0.

## Keeping it in step with celestron-aux

The payload decoders are hand-written C, one per Python decoder in
`celestron_aux/decoding.py` and with the same name (`_motor_position` becomes
`decode_motor_position`). The tables between the `BEGIN GENERATED` and
`END GENERATED` markers (device and command names, mount models, and which
decoder each command uses) are written from the catalog:

```bash
python -m celestron_aux.wireshark_c
```

`tests/test_wireshark_c.py` fails when those tables are out of date or a Python
decoder has no C port. With a tshark built as below, it also checks that the C
decoders give exactly the same text as Python on over 27,000 payloads, that
live TCP traffic is reassembled, that the heuristic works, and that random bytes
cannot crash the dissector:

```bash
CELESTRON_AUX_TSHARK=~/claudeprojects/wireshark/build/run/tshark .venv/bin/python -m pytest tests/test_wireshark_c.py
```

## Building tshark with it

```bash
git clone https://gitlab.com/wireshark/wireshark.git
cd wireshark
ln -s /path/to/celestron-aux/wireshark/packet-celestron-aux.c epan/dissectors/
```

Add the file to `epan/dissectors/CMakeLists.txt`, in alphabetical order, just
before `packet-cell_broadcast.c`:

```cmake
	${CMAKE_CURRENT_SOURCE_DIR}/packet-celestron-aux.c
```

Then build only tshark:

```bash
mkdir build && cd build
cmake -G Ninja -DBUILD_wireshark=OFF -DBUILD_stratoshark=OFF -DBUILD_logray=OFF ..
ninja tshark
```

Wireshark needs GLib, libgcrypt, c-ares and PCRE2. On a Mac with an Intel
Homebrew, which cannot build libgcrypt from source any more, build libgpg-error,
libgcrypt and c-ares from their release tarballs with `CC="cc -arch x86_64"`
into a folder of their own and pass it with `-DCMAKE_PREFIX_PATH=...`.

Run Wireshark's own checks on the file before submitting:

```bash
python3 tools/check_dissector.py --file epan/dissectors/packet-celestron-aux.c
```

## Submitting it to Wireshark

Wireshark takes contributions as merge requests on GitLab
(https://gitlab.com/wireshark/wireshark), following the Developer's Guide
(https://www.wireshark.org/docs/wsdg_html/#ChSrcContribute).

1. Fork the repository on GitLab and make a branch from `master`.
2. Copy `packet-celestron-aux.c` into `epan/dissectors/` (a copy, not a link).
3. Add it to `epan/dissectors/CMakeLists.txt` as above.
4. Add a line to the "New Protocol Support" list in
   `doc/Wireshark_Release_Notes.adoc`, in alphabetical order:
   `Celestron AUX bus (Celestron AUX)`.
5. Commit with a message in Wireshark's style, for example:

   ```
   Celestron AUX: Add dissector

   Add a dissector for the Celestron AUX bus, used between the parts of
   Celestron telescope mounts and carried over TCP by their Wi-Fi modules.
   ```

6. Open a merge request. Reviewers usually ask for a sample capture: attach a
   small pcap to the merge request or to a GitLab issue (for example a bus scan
   and a few go-tos over Wi-Fi), with nothing private in it.
