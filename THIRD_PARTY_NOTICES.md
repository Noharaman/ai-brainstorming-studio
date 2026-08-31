# Third-Party Notices

This project depends on the packages listed below. It does **not** bundle or
redistribute them: they are installed from PyPI by the user (`pip install -r
requirements.txt`).

If you produce a binary distribution (for example a `.app` bundle built with
`scripts/build_macos_app.py`), regenerate this list from what that build
actually embeds, including the transitive dependencies and their exact
versions. The list below covers the direct dependencies of the source
distribution only.

---

## CustomTkinter

- License: MIT
- Homepage: https://github.com/TomSchimansky/CustomTkinter
- Declared in `requirements.txt` as `customtkinter>=5.2.2`

```
MIT License

Copyright (c) 2023 Tom Schimansky

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

---

## Pillow

- License: MIT-CMU
- Homepage: https://github.com/python-pillow/Pillow
- Declared in `requirements.txt` as `Pillow>=10.0.0`

```
The Python Imaging Library (PIL) is

    Copyright © 1997-2011 by Secret Labs AB
    Copyright © 1995-2011 by Fredrik Lundh and contributors

Pillow is the friendly PIL fork. It is

    Copyright © 2010 by Jeffrey A. Clark and contributors

Like PIL, Pillow is licensed under the open source MIT-CMU License:

By obtaining, using, and/or copying this software and/or its associated
documentation, you agree that you have read, understood, and will comply
with the following terms and conditions:

Permission to use, copy, modify and distribute this software and its
documentation for any purpose and without fee is hereby granted, provided
that the above copyright notice appears in all copies, and that both that
copyright notice and this permission notice appear in supporting
documentation, and that the name of Secret Labs AB or the author not be used
in advertising or publicity pertaining to distribution of the software
without specific, written prior permission.

SECRET LABS AB AND THE AUTHOR DISCLAIMS ALL WARRANTIES WITH REGARD TO THIS
SOFTWARE, INCLUDING ALL IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS.
IN NO EVENT SHALL SECRET LABS AB OR THE AUTHOR BE LIABLE FOR ANY SPECIAL,
INDIRECT OR CONSEQUENTIAL DAMAGES OR ANY DAMAGES WHATSOEVER RESULTING FROM
LOSS OF USE, DATA OR PROFITS, WHETHER IN AN ACTION OF CONTRACT, NEGLIGENCE
OR OTHER TORTIOUS ACTION, ARISING OUT OF OR IN CONNECTION WITH THE USE OR
PERFORMANCE OF THIS SOFTWARE.
```

---

## Python standard library

`tkinter` is part of the Python standard library and is used through
CustomTkinter. Python is distributed under the PSF License Agreement:
https://docs.python.org/3/license.html

---

## External tools that are NOT bundled

This project can, in builds where those slots are enabled, invoke the
following command-line tools. **None of them is bundled, redistributed, or
modified by this project.** Each must be installed and authenticated by the
user, under that vendor's own terms.

| Tool | Vendor |
| --- | --- |
| Claude Code (`claude`) | Anthropic |
| Antigravity CLI (`agy`) | Google |
| Codex CLI (`codex`) | OpenAI |
| LM Studio | Element Labs |

In the current build all three AI CLI slots are disabled and none of those
CLIs is launched; see `README.md`. LM Studio runs as a local server the user
starts themselves.

### Trademarks

Claude, Anthropic, Google, Antigravity, OpenAI, Codex, ChatGPT, LM Studio and
all other product names, logos, and brands are the property of their
respective owners. Use of these names is for identification purposes only and
does not imply endorsement by, affiliation with, or sponsorship from any of
them. This project is an independent work and is not affiliated with
Anthropic, Google, OpenAI, or Element Labs.
