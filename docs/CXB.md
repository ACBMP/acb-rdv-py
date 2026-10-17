# Game settings (CXB)

ACB downloads its multiplayer settings at login, through
`PersistentStore.GetItem`, as one CXB file. The files inside it are game
modes, XP tables, score bonuses and so on:

- each is an XML document;
- each is minified and LZO2A-compressed;
- they are stored in a name/size table.

The goal is to keep only the XML sources and build the CXB when it's needed.

## How the server gets the CXB

`game_settings` decides the source:

| `game_settings` points to | Provider | Behaviour |
|---|---|---|
| a directory (the settings submodule, default `gamesettings/`) | `XmlSettingsProvider` | Encodes every `*.xml` with the CXB library on the first request, and again after any file changes (checked at most every 2 s). |
| a file | `CxbFileProvider` | Serves a prebuilt `.cxb`, re-read when it changes. This is today's setup. |

The XML provider has these rules:

- **Files included.** Only `*.xml` files at the top level of the directory are
  used.
- **Order.** Files are ordered by base name in byte order, which is what
  CXBTool `convert` does (`std::map`).
- **Bytes.** Documents are passed as raw bytes. Each declares its own encoding
  (the repo copies use ISO-8859-1).
- **Validation.** Every document is checked for well-formedness in Python
  first. If any document is broken, nothing is encoded.
- **Failed rebuilds.** A failed rebuild (bad XML, library error) keeps serving
  the last good CXB. `GET /settings` on the status port shows the source, size,
  SHA-1, file list and the last error.
- **Startup.** The first build happens at server start. If it fails, the error
  is logged loudly and players get an empty file (acb-rdv's behaviour) until
  the XMLs are fixed.
- **Cost.** Encoding runs on a worker thread, so it never blocks the event loop.

## The library contract

The server loads the library with `ctypes` and needs the plain C ABI in
[`native/cxb.h`](../native/cxb.h):

```c
int  cxb_abi_version(void);                    /* must return CXB_ABI_VERSION (1) */
int  cxb_encode(const cxb_file *files, size_t count, uint8_t **out, size_t *out_len);
void cxb_free(void *buffer);
const char *cxb_last_error(void);              /* per thread, never NULL */
```

Requirements the server relies on:

- **Symbols.** Export the four `extern "C"` symbols from a shared library:
  `libcxb.so`, or `cxb.dll` on Windows. No C++ types or exceptions may cross
  the boundary.
- **Order.** `cxb_encode` must encode the files in the order given.
- **Errors.** `cxb_encode` must fail (non-zero, with a message) on XML that
  does not parse. Today's `ConvertToCXB` ignores `load_string` errors and would
  encode an empty document.
- **Memory.** The output buffer is allocated by the library and released with
  `cxb_free`.
- **Threads.** `cxb_encode` must be safe on any thread.
- **Linking.** Link LZO and pugixml statically into the library if possible,
  so deploying it means copying one file.
- **Versioning.** Bump `CXB_ABI_VERSION` when a signature or behaviour
  changes. The server refuses a version it doesn't know.

**Reference implementation.**
[`tests/native/cxb_reference.cpp`](../tests/native/cxb_reference.cpp)
implements the contract over CXBTool's current `ConvertToCXB`. The tests
compile it against `../CXBTool/src/cxb_parser.cpp`, then check two things:

- a reference `.cxb`, exported to XML and encoded through the ctypes path,
  comes out byte-identical to the original;
- malformed XML is rejected.

It is test-only; the real library comes from the CXBTool refactor.

## Switching over

1. Build libcxb from the refactored CXBTool.
2. Check it against the contract:
   ```sh
   ACB_CXB_LIBRARY=/path/to/libcxb.so python3 -c \
     "from acb_rdv.settings import find_encoder; print(find_encoder().name)"
   ```
   Also run `pytest tests/test_settings.py`, which exercises the reference
   implementation.

   For Docker, build the library inside (or for) the image's Debian base, or
   link it fully statically. A library built on the Arch host may need a newer
   glibc than `python:3.13-slim` has.
3. Add the settings repository as a submodule at `gamesettings/`:
   `git submodule add <url> gamesettings`.
4. Set `game_settings = "gamesettings"` and `cxb_library`, or the matching
   `ACB_RDV_*` variables. `docker-compose.yml` has the lines ready: mount the
   submodule read-only and the built library, then switch
   `ACB_RDV_GAME_SETTINGS`.
5. Check `GET /settings`. A build from unchanged XML should have the same
   SHA-1 as the `.cxb` it replaces.

## Things to keep in mind

- **Reported file size.** `GameInfo.GetFileListOnGameStorage` reports a file
  size of 58484 bytes, a value dumped from the original service. acb-rdv always
  sent it, even though its file is 55796 bytes, so clients cope with a smaller
  file. Nobody knows whether they cope with a larger one. The server warns at
  start if the CXB exceeds `game_storage_file_size`; raise that value if edits
  grow the settings.
- **Session checksum.** Game sessions carry a `CxbCrcSum` attribute (0x64),
  computed by the clients. Every client downloads the same CXB at login, so
  they agree. Players who logged in before an edit keep the old settings (and
  checksum) until they log in again.
