// Test-only implementation of native/cxb.h on top of CXBTool's current
// ConvertToCXB (src/cxb_parser.cpp). It lets the server's library path be
// tested before the real CXB library exists; the tests compile it on the fly.
// Not shipped - the real library comes from the CXBTool refactor.

#include "../../native/cxb.h"

#include <cstdlib>
#include <cstring>
#include <exception>
#include <string>
#include <vector>

#include <pugixml.hpp>

#include "cxb_parser.hpp"

namespace {
thread_local std::string last_error = "no error";
}

extern "C" {

int cxb_abi_version(void) { return CXB_ABI_VERSION; }

int cxb_encode(const cxb_file *files, size_t count, uint8_t **out, size_t *out_len) {
    try {
        std::vector<CXBFile> input;
        for (size_t i = 0; i < count; ++i) {
            std::string xml(reinterpret_cast<const char *>(files[i].xml), files[i].xml_len);
            pugi::xml_document doc;
            pugi::xml_parse_result parsed = doc.load_buffer(xml.data(), xml.size());
            if (!parsed) {
                last_error = std::string(files[i].name) + ": " + parsed.description();
                return 1;
            }
            input.push_back(CXBFile{files[i].name, xml});
        }
        std::vector<uint8_t> cxb = ConvertToCXB(input);
        auto *buffer = static_cast<uint8_t *>(std::malloc(cxb.size()));
        if (buffer == nullptr) {
            last_error = "out of memory";
            return 1;
        }
        std::memcpy(buffer, cxb.data(), cxb.size());
        *out = buffer;
        *out_len = cxb.size();
        return 0;
    } catch (const std::exception &e) {
        last_error = e.what();
        return 1;
    }
}

void cxb_free(void *buffer) { std::free(buffer); }

const char *cxb_last_error(void) { return last_error.c_str(); }
}
