/*
 * C ABI the ACB server expects from the CXB library (CXBTool as a library).
 *
 * The server loads the library with ctypes (acb_rdv/settings/encoders.py) and
 * calls it whenever the settings XMLs change, so it needs a plain C interface:
 * no C++ types or exceptions may cross it.
 *
 * Build it as a shared library named libcxb.so (Linux) / cxb.dll (Windows)
 * exporting these symbols.
 */
#ifndef CXB_H
#define CXB_H

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* Bump when a signature or the meaning of a call changes. */
#define CXB_ABI_VERSION 1

#if defined(_WIN32)
#  define CXB_API __declspec(dllexport)
#else
#  define CXB_API __attribute__((visibility("default")))
#endif

typedef struct cxb_file {
    /* File name such as "globalparams.xml"; the CXB records the part before
       the first '.' (as CXBTool's ConvertToCXB does). NUL-terminated UTF-8. */
    const char *name;
    /* The XML document exactly as stored on disk (any declared encoding);
       not NUL-terminated. */
    const uint8_t *xml;
    size_t xml_len;
} cxb_file;

/* Returns CXB_ABI_VERSION of the library. The server refuses a mismatch. */
CXB_API int cxb_abi_version(void);

/*
 * Encode `count` XML documents, in the given order, into one CXB file.
 * The caller orders them as CXBTool's `convert` does (by base name, byte order).
 *
 * Returns 0 on success: *out points to a buffer of *out_len bytes allocated by
 * the library, to be released with cxb_free().
 * Returns non-zero on failure, including an XML document that does not
 * parse (it must not be encoded as an empty document). *out is then left
 * unchanged and cxb_last_error() describes the problem.
 *
 * Must be safe to call from any thread (the server encodes on a worker thread).
 */
CXB_API int cxb_encode(const cxb_file *files, size_t count, uint8_t **out, size_t *out_len);

/* Releases a buffer returned by cxb_encode(). NULL is ignored. */
CXB_API void cxb_free(void *buffer);

/* Message for the last failed call on the calling thread; valid until the next
   call on that thread. Never NULL. */
CXB_API const char *cxb_last_error(void);

#ifdef __cplusplus
}
#endif

#endif /* CXB_H */
