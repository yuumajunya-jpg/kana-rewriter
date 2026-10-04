#pragma once
#include <windows.h>
#include <algorithm>
#include <chrono>
#include <cstdint>
#include <iostream>
#include <limits>
#include <stdexcept>
#include <string>
#include <vector>

namespace kana {
using Clock = std::chrono::steady_clock;
using Deadline = Clock::time_point;
constexpr size_t max_frame = 16 * 1024 * 1024;
inline void require(bool ok, const char* message) {
    if (!ok) throw std::runtime_error(message);
}
inline void hr(HRESULT result, const char* message) { require(SUCCEEDED(result), message); }

inline std::wstring wide(const std::string& text) {
    if (text.empty()) return {};
    require(text.size() <= max_frame, "Text too large");
    int count = MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, text.data(), int(text.size()), nullptr, 0);
    require(count > 0, "Invalid UTF-8");
    std::wstring result(count, 0);
    require(MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, text.data(), int(text.size()), result.data(), count) == count,
            "Invalid UTF-8");
    return result;
}
inline std::string utf8(const std::wstring& text) {
    if (text.empty()) return {};
    int count = WideCharToMultiByte(CP_UTF8, WC_ERR_INVALID_CHARS, text.data(), int(text.size()), nullptr, 0, nullptr, nullptr);
    require(count > 0, "Invalid UTF-16");
    std::string result(count, 0);
    require(WideCharToMultiByte(CP_UTF8, WC_ERR_INVALID_CHARS, text.data(), int(text.size()), result.data(), count, nullptr, nullptr) == count,
            "Invalid UTF-16");
    return result;
}
inline bool high(wchar_t ch) { return ch >= 0xd800 && ch <= 0xdbff; }
inline bool low(wchar_t ch) { return ch >= 0xdc00 && ch <= 0xdfff; }
// All IPC offsets count Unicode scalar values, matching Python str.
inline size_t unit_offset(const std::wstring& text, size_t points) {
    size_t i = 0, count = 0;
    while (i < text.size() && count < points) {
        if (high(text[i])) {
            require(i + 1 < text.size() && low(text[i + 1]), "Invalid surrogate pair");
            i += 2;
        } else {
            require(!low(text[i]), "Invalid surrogate pair");
            ++i;
        }
        ++count;
    }
    require(count == points, "Character offset outside document");
    return i;
}
inline size_t point_offset(const std::wstring& text, size_t units) {
    require(units <= text.size(), "UTF-16 offset outside document");
    size_t count = 0;
    for (size_t i = 0; i < units; ++i, ++count) {
        if (high(text[i])) {
            require(i + 1 < units && low(text[i + 1]), "Offset splits surrogate pair");
            ++i;
        } else require(!low(text[i]), "Invalid surrogate pair");
    }
    return count;
}
inline size_t points(const std::wstring& text) { return point_offset(text, text.size()); }
inline std::wstring normalize(const std::wstring& text, wchar_t newline = L'\n') {
    std::wstring result;
    result.reserve(text.size());
    for (size_t i = 0; i < text.size(); ++i) {
        if (text[i] == L'\r') {
            if (i + 1 < text.size() && text[i + 1] == L'\n') ++i;
            result += newline;
        } else result += text[i];
    }
    return result;
}
inline bool starts(const std::wstring& text, const std::wstring& prefix) {
    return text.size() >= prefix.size() && text.compare(0, prefix.size(), prefix) == 0;
}
struct State {
    std::wstring text;
    size_t start = 0, end = 0; // Unicode scalar offsets
    bool operator==(const State& rhs) const { return text == rhs.text && start == rhs.start && end == rhs.end; }
    bool operator!=(const State& rhs) const { return !(*this == rhs); }
};
struct Config {
    std::string backend = "auto", ime = "auto", wait = "poll";
    uint64_t timeout_ms = 5000, limit = 200000, initial_delay_ms = 0;
    bool timing = false;
};
// A count: name uses the numeric field as a count instead of microseconds.
// The record layout remains compatible with existing KRN1 workers.
struct Timing { std::string name; uint64_t us; };
inline thread_local std::vector<Timing> timings;
inline thread_local bool measure = false;
struct Stage {
    std::string name;
    Deadline start;
    explicit Stage(const char* value) : name(measure ? value : ""), start(measure ? Clock::now() : Deadline{}) {}
    ~Stage() {
        if (measure) timings.push_back({name, uint64_t(std::chrono::duration_cast<std::chrono::microseconds>(Clock::now() - start).count())});
    }
};
struct Writer {
    std::string data;
    void number(uint64_t value) {
        for (int i = 0; i < 8; ++i) data += char((value >> (i * 8)) & 255);
    }
    void text(const std::string& value) { number(value.size()); data += value; }
    void text(const std::wstring& value) { text(utf8(value)); }
};
struct Reader {
    const std::string& data;
    size_t offset = 0;
    uint64_t number() {
        require(data.size() - offset >= 8, "Truncated request");
        uint64_t value = 0;
        for (int i = 0; i < 8; ++i) value |= uint64_t(uint8_t(data[offset++])) << (i * 8);
        return value;
    }
    std::string text() {
        uint64_t size = number();
        require(size <= data.size() - offset, "Truncated text");
        auto value = data.substr(offset, size);
        offset += size;
        return value;
    }
    void done() { require(offset == data.size(), "Unexpected request fields"); }
};
inline bool read_frame(std::string& data) {
    char header[8];
    std::cin.read(header, 8);
    if (std::cin.gcount() == 0 && std::cin.eof()) return false;
    require(std::cin.gcount() == 8 && std::string(header, 4) == "KRN1", "Invalid IPC header");
    uint32_t size = 0;
    for (int i = 0; i < 4; ++i) size |= uint32_t(uint8_t(header[4 + i])) << (i * 8);
    require(size <= max_frame, "IPC request too large");
    data.resize(size);
    std::cin.read(data.data(), size);
    require(size_t(std::cin.gcount()) == size, "Truncated IPC frame");
    return true;
}
inline void write_frame(const Writer& writer) {
    require(writer.data.size() <= max_frame, "IPC response too large");
    char header[8] = {'K', 'R', 'N', '1', 0, 0, 0, 0};
    auto size = uint32_t(writer.data.size());
    for (int i = 0; i < 4; ++i) header[4 + i] = char((size >> (i * 8)) & 255);
    std::cout.write(header, 8);
    std::cout.write(writer.data.data(), writer.data.size());
    std::cout.flush();
    require(bool(std::cout), "IPC output closed");
}
} // namespace kana
