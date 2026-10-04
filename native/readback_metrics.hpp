#pragma once
#include "protocol.hpp"
#include <array>

namespace kana {
// Aggregate in memory and publish once after readback, including failures.
// Phase durations partition elapsed time; milestones overlap those phases.
class ReadbackMetrics {
public:
    enum Phase { Focus, TextQuery, StateCheck, PollWait, PhaseCount };
    bool enabled = measure;
    uint64_t probes = 0, text_misses = 0, caret_misses = 0, unstable = 0;
    bool event_mode = false, event_subscribed = false;
    uint64_t event_notifications = 0;
private:
    Deadline start_ = enabled ? Clock::now() : Deadline{};
    std::array<Clock::duration, PhaseCount> phases_{};
    bool text_seen_ = false, confirmed_ = false;
    Clock::duration first_text_{}, confirmation_{};
    static uint64_t us(Clock::duration value) {
        return uint64_t(std::chrono::duration_cast<std::chrono::microseconds>(value).count());
    }
public:
    class Slice {
        ReadbackMetrics& metrics_;
        Phase phase_;
        Deadline start_;
    public:
        Slice(ReadbackMetrics& metrics, Phase phase)
            : metrics_(metrics), phase_(phase), start_(metrics.enabled ? Clock::now() : Deadline{}) {}
        Slice(const Slice&) = delete;
        ~Slice() { if (metrics_.enabled) metrics_.phases_[phase_] += Clock::now() - start_; }
    };
    void text_match() {
        if (enabled && !text_seen_) { text_seen_ = true; first_text_ = Clock::now() - start_; }
    }
    void confirmed() {
        if (enabled) { confirmed_ = true; confirmation_ = Clock::now() - start_; }
    }
    ~ReadbackMetrics() {
        if (!enabled) return;
        auto elapsed = Clock::now() - start_;
        Clock::duration accounted{};
        const char* names[]{"uia_readback_focus", "uia_readback_text_query",
                            "uia_readback_state_check", "uia_readback_poll_wait"};
        for (size_t i = 0; i < PhaseCount; ++i) {
            timings.push_back({names[i], us(phases_[i])}); accounted += phases_[i];
        }
        timings.push_back({"uia_readback_other", us(std::max(Clock::duration{}, elapsed - accounted))});
        if (text_seen_) timings.push_back({"uia_readback_first_text_match", us(first_text_)});
        if (confirmed_) timings.push_back({"uia_readback_confirmed", us(confirmation_)});
        timings.push_back({"count:uia_readback_probes", probes});
        timings.push_back({"count:uia_readback_text_misses", text_misses});
        timings.push_back({"count:uia_readback_caret_misses", caret_misses});
        timings.push_back({"count:uia_readback_unstable", unstable});
        timings.push_back({"count:uia_readback_text_seen", text_seen_});
        timings.push_back({"count:uia_readback_confirmed", confirmed_});
        timings.push_back({"count:uia_readback_event_mode", event_mode});
        timings.push_back({"count:uia_readback_event_subscribed", event_subscribed});
        timings.push_back({"count:uia_readback_event_notifications", event_notifications});
    }
};
} // namespace kana
