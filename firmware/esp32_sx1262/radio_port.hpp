#pragma once

#include "radio_config_guard.hpp"

#include <cstddef>
#include <cstdint>

namespace nexalert {

enum class RadioState : uint8_t {
  Uninitialized,
  Ready,
  Disconnected,
  InitializationFailed,
};

struct RadioTxResult {
  bool tx_done;
  // Null/unknown is represented separately from a hop acknowledgement.
  bool hop_ack_known;
  bool hop_ack;
};

struct RadioPacket {
  uint8_t bytes[240];
  size_t length;
};

// Driver-specific implementation must not set a region or pin map implicitly.
class RadioPort {
 public:
  virtual ~RadioPort() = default;
  virtual RadioState initialize() = 0;
  virtual RadioState state() const = 0;
  virtual RadioTxResult send(const uint8_t* data, size_t length) = 0;
  // Returns false when no complete packet was received before timeout.
  virtual bool receive(RadioPacket& packet, uint32_t timeout_ms) = 0;
};

}  // namespace nexalert
