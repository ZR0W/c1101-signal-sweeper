/*
  CC1101 signal scanner — Arduino Nano ESP32
  Finds transmitters (weather stations, sensors, remotes, doorbells...) and
  captures their pulses so you can see how they encode data.

  Library: SmartRC-CC1101-Driver-Lib version 2.5.7 (Library Manager)
  Board:   Arduino Nano ESP32, Tools > Pin Numbering > "By GPIO number (legacy)"
  Wiring:  same as the PixMob project
    GND -> 1 GND     3V3 -> 2 VCC     D2  -> 3 GDO0    D10 -> 4 CSN
    D13 -> 5 SCK     D11 -> 6 MOSI    D12 -> 7 MISO    (8 GDO2 unused)

  Serial Monitor: 115200 baud, line ending "Newline". Send any key to stop.

    scan 433               sweep a preset band for 10 s (max-hold), show peaks
    scan 915 60            ...for 60 s (sensors often transmit every 30-60 s)
    scan 430 440 50 20     custom: start MHz, end MHz, step kHz, seconds
    listen 433.92          capture bursts at one frequency (OOK/ASK)
    listen 915.0 fsk       ...for FSK devices
    listen 433.92 -80      ...with a manual trigger level
    rssi 433.92            live signal-strength meter
    bands                  list presets          ?   help

  Receive only: this sketch never transmits.
*/

#include <algorithm>
#include <ELECHOUSE_CC1101_SRC_DRV.h>

// GPIO numbers (Nano ESP32 labels in comments)
#define PIN_GDO0 5    // D2
#define PIN_CSN  21   // D10
#define PIN_MOSI 38   // D11
#define PIN_MISO 47   // D12
#define PIN_SCK  48   // D13

struct Band { const char *name; float start, end; };
const Band kBands[] = {
  {"315", 310.0, 320.0},   // older US remotes, car/garage (receive only!)
  {"433", 430.0, 437.0},   // weather stations, doorbells, outlets
  {"868", 863.0, 870.0},   // EU sensors, EU PixMob
  {"915", 902.0, 928.0},   // US sensors, LoRa, US PixMob
};

const int kMaxBins = 400;
int8_t peakRssi[kMaxBins];

// ---------------------------------------------------------------- radio

bool validFreq(float f) {
  return (f >= 300 && f <= 348) || (f >= 387 && f <= 464) || (f >= 779 && f <= 928);
}

bool radioInit() {
  ELECHOUSE_cc1101.setSpiPin(PIN_SCK, PIN_MISO, PIN_MOSI, PIN_CSN);
  if (!ELECHOUSE_cc1101.getCC1101()) return false;
  ELECHOUSE_cc1101.Init();
  ELECHOUSE_cc1101.setCCMode(0);        // async: demodulated data out on GDO0
  ELECHOUSE_cc1101.setModulation(2);    // ASK/OOK
  ELECHOUSE_cc1101.setMHZ(433.92);
  pinMode(PIN_GDO0, INPUT);
  return true;
}

bool stopRequested() {
  if (!Serial.available()) return false;
  while (Serial.available()) Serial.read();
  return true;
}

// ---------------------------------------------------------------- scan

void scan(float f0, float f1, float stepKHz, uint32_t seconds) {
  int bins = (int)((f1 - f0) * 1000.0 / stepKHz) + 1;
  if (bins > kMaxBins) {
    stepKHz = (f1 - f0) * 1000.0 / (kMaxBins - 1);
    bins = kMaxBins;
  }
  for (int i = 0; i < bins; i++) peakRssi[i] = -128;

  ELECHOUSE_cc1101.setModulation(2);
  ELECHOUSE_cc1101.setRxBW(stepKHz >= 100 ? 101.56 : 58.04);
  Serial.printf("\nScanning %.2f-%.2f MHz, %d steps of %.0f kHz, %lu s (any key stops)\n",
                f0, f1, bins, stepKHz, (unsigned long)seconds);

  uint32_t t0 = millis(), lastDot = t0;
  int passes = 0;
  bool aborted = false;
  while (millis() - t0 < seconds * 1000UL && !aborted) {
    for (int i = 0; i < bins; i++) {
      ELECHOUSE_cc1101.SetRx(f0 + i * stepKHz / 1000.0);
      delayMicroseconds(1000);           // PLL calibration + RSSI settling
      int r = ELECHOUSE_cc1101.getRssi();
      if (r > peakRssi[i]) peakRssi[i] = r;
    }
    passes++;
    if (millis() - lastDot > 1000) { Serial.print('.'); lastDot = millis(); }
    aborted = stopRequested();
  }
  ELECHOUSE_cc1101.setSidle();
  Serial.printf("\n%d sweeps done%s\n", passes, aborted ? " (stopped early)" : "");

  // Noise floor = median of the max-hold values
  int8_t sorted[kMaxBins];
  memcpy(sorted, peakRssi, bins);
  std::sort(sorted, sorted + bins);
  int floorDb = sorted[bins / 2];
  int threshold = floorDb + 10;

  // Bar chart, grouped to at most 32 rows
  const int rows = min(bins, 32);
  const int per = (bins + rows - 1) / rows;
  Serial.printf("Noise floor %d dBm. Bars: -110 to -30 dBm, '*' = %d dBm or more\n",
                floorDb, threshold);
  for (int r = 0; r * per < bins; r++) {
    int best = -128, bestI = r * per;
    for (int i = r * per; i < min(bins, (r + 1) * per); i++) {
      if (peakRssi[i] > best) { best = peakRssi[i]; bestI = i; }
    }
    int len = constrain(map(best, -110, -30, 0, 40), 0, 40);
    char bar[42];
    memset(bar, best >= threshold ? '*' : '#', len);
    bar[len] = 0;
    Serial.printf("%8.3f MHz %4d dBm |%s\n", f0 + bestI * stepKHz / 1000.0, best, bar);
  }

  // Top peaks (local maxima above threshold)
  Serial.println(F("Strongest signals:"));
  int found = 0;
  bool used[kMaxBins] = {false};
  for (int k = 0; k < 5; k++) {
    int best = -128, bestI = -1;
    for (int i = 0; i < bins; i++) {
      if (!used[i] && peakRssi[i] >= threshold && peakRssi[i] > best) { best = peakRssi[i]; bestI = i; }
    }
    if (bestI < 0) break;
    for (int i = max(0, bestI - 3); i <= min(bins - 1, bestI + 3); i++) used[i] = true;
    float f = f0 + bestI * stepKHz / 1000.0;
    Serial.printf("  %.3f MHz  %d dBm  (+%d dB)  -> try: listen %.3f\n",
                  f, best, best - floorDb, f);
    found++;
  }
  if (!found) {
    Serial.println(F("  none above the noise. Scan longer, move closer, or press"));
    Serial.println(F("  a remote button / wait for a sensor during the scan."));
  }
}

// ---------------------------------------------------------------- listen

const int kEdgeBuf = 4096;
volatile uint32_t edgeTime[kEdgeBuf];
volatile uint8_t  edgeLevel[kEdgeBuf];
volatile int      edgeCount = 0;
volatile bool     recording = false;

void IRAM_ATTR onEdge() {
  if (!recording || edgeCount >= kEdgeBuf) return;
  edgeTime[edgeCount] = micros();
  edgeLevel[edgeCount] = digitalRead(PIN_GDO0);
  edgeCount++;
}

// Group sorted durations into clusters; returns number of clusters.
int clusterWidths(uint32_t *v, int n, uint32_t *centers, int *counts, int maxC) {
  std::sort(v, v + n);
  int c = 0;
  uint32_t sum = 0;
  int cnt = 0;
  for (int i = 0; i < n; i++) {
    if (cnt && v[i] > v[i - 1] * 13 / 10) {
      if (c < maxC) { centers[c] = sum / cnt; counts[c] = cnt; }
      c++; sum = 0; cnt = 0;
    }
    sum += v[i]; cnt++;
  }
  if (cnt) { if (c < maxC) { centers[c] = sum / cnt; counts[c] = cnt; } c++; }
  return c;
}

void printClusters(const char *label, uint32_t *centers, int *counts, int c) {
  Serial.printf("  %s:", label);
  for (int i = 0; i < min(c, 4); i++) Serial.printf("  ~%lu us x%d", (unsigned long)centers[i], counts[i]);
  if (c > 4) Serial.printf("  (+%d more)", c - 4);
  Serial.println();
}

void printBits(const String &bits) {
  Serial.printf("  %d bits: ", bits.length());
  for (unsigned i = 0; i < bits.length(); i++) {
    Serial.print(bits[i]);
    if (i % 8 == 7) Serial.print(' ');
  }
  Serial.print("\n  hex: ");
  for (unsigned i = 0; i + 4 <= bits.length(); i += 4) {
    Serial.print(strtol(bits.substring(i, i + 4).c_str(), nullptr, 2), HEX);
  }
  Serial.println();
}

void analyzeBurst(float mhz, int peak, uint32_t durationMs) {
  static int burstNo = 0;
  int n = edgeCount;
  static int32_t pulses[kEdgeBuf];
  static uint32_t highs[kEdgeBuf], lows[kEdgeBuf];
  int np = 0, nh = 0, nl = 0;

  for (int i = 1; i < n; i++) {
    uint32_t d = edgeTime[i] - edgeTime[i - 1];
    if (d < 40) continue;                 // ignore glitches
    bool high = edgeLevel[i - 1];
    pulses[np++] = high ? (int32_t)d : -(int32_t)d;
    if (high) highs[nh++] = d; else lows[nl++] = d;
  }

  burstNo++;
  Serial.printf("\n[%.1f s] burst #%d at %.3f MHz, peak %d dBm, %lu ms, %d pulses\n",
                millis() / 1000.0, burstNo, mhz, peak, (unsigned long)durationMs, np);
  if (np < 8) { Serial.println(F("  too short to analyze (noise or a blip)")); return; }

  uint32_t hc[8], lc[8];
  int hn[8], ln[8];
  int hC = clusterWidths(highs, nh, hc, hn, 8);
  int lC = clusterWidths(lows, nl, lc, ln, 8);
  printClusters("high widths", hc, hn, hC);
  printClusters("gap widths ", lc, ln, lC);

  Serial.print("  raw:");
  for (int i = 0; i < min(np, 32); i++) Serial.printf(" %+ld", (long)pulses[i]);
  if (np > 32) Serial.print(" ...");
  Serial.println();

  // Guess the encoding from the two biggest clusters
  if (hC >= 2 && hn[0] > 3 && hn[1] > 3) {
    uint32_t mid = (hc[0] + hc[1]) / 2;
    String bits;
    for (int i = 0; i < np; i++) if (pulses[i] > 0) bits += (pulses[i] > (int32_t)mid) ? '1' : '0';
    Serial.println(F("  looks like PWM (pulse width = bit, long = 1):"));
    printBits(bits);
  } else if (lC >= 2 && hC == 1 && ln[0] > 3 && ln[1] > 3) {
    uint32_t mid = (lc[0] + lc[1]) / 2;
    String bits;
    for (int i = 0; i < np; i++) if (pulses[i] < 0 && -pulses[i] < (int32_t)lc[1] * 2) {
      bits += (-pulses[i] > (int32_t)mid) ? '1' : '0';
    }
    Serial.println(F("  looks like PPM (gap width = bit, long = 1):"));
    printBits(bits);
  } else {
    Serial.println(F("  encoding: Manchester, FSK or unknown (see raw timings)"));
  }
}

void listen(float mhz, bool fsk, int manualTrigger) {
  ELECHOUSE_cc1101.setModulation(fsk ? 0 : 2);
  if (fsk) ELECHOUSE_cc1101.setDeviation(47.6);
  ELECHOUSE_cc1101.setRxBW(101.56);       // narrower = lower noise floor
  ELECHOUSE_cc1101.SetRx(mhz);
  delay(5);

  // Measure the noise floor for 300 ms
  long sum = 0;
  int samples = 0;
  uint32_t t0 = millis();
  while (millis() - t0 < 300) { sum += ELECHOUSE_cc1101.getRssi(); samples++; delay(2); }
  int floorDb = sum / samples;
  int threshold = manualTrigger ? manualTrigger : floorDb + 6;
  Serial.printf("\nListening on %.3f MHz (%s). Noise %d dBm, trigger %d dBm. Any key stops.\n",
                mhz, fsk ? "FSK" : "OOK", floorDb, threshold);
  Serial.println(F("A status line every 5 s shows the strongest signal seen."));

  attachInterrupt(digitalPinToInterrupt(PIN_GDO0), onEdge, CHANGE);
  bool inBurst = false;
  uint32_t start = 0, lastAbove = 0;
  int peak = -128;
  int windowMax = -128;
  uint32_t lastStatus = millis();

  while (!stopRequested()) {
    int r = ELECHOUSE_cc1101.getRssi();
    uint32_t now = millis();
    if (r > windowMax) windowMax = r;
    if (!inBurst && now - lastStatus >= 5000) {
      Serial.printf("  ...waiting. strongest in last 5 s: %d dBm (trigger %d)%s\n", windowMax,
                    threshold, windowMax >= threshold - 4 && windowMax < threshold
                    ? "  <- close! try a lower trigger" : "");
      windowMax = -128;
      lastStatus = now;
    }
    if (r >= threshold) {
      if (!inBurst) {
        inBurst = true;
        edgeCount = 0;
        recording = true;
        start = now;
        peak = r;
      }
      lastAbove = now;
      if (r > peak) peak = r;
    } else if (inBurst && now - lastAbove > 15) {
      recording = false;
      inBurst = false;
      analyzeBurst(mhz, peak, lastAbove - start);
    }
    if (inBurst && edgeCount >= kEdgeBuf) {   // buffer full: analyze what we have
      recording = false;
      inBurst = false;
      analyzeBurst(mhz, peak, now - start);
      Serial.println(F("  (buffer full: very long or continuous signal)"));
    }
    delayMicroseconds(300);
  }
  recording = false;
  detachInterrupt(digitalPinToInterrupt(PIN_GDO0));
  ELECHOUSE_cc1101.setSidle();
  Serial.println(F("Stopped."));
}

// ---------------------------------------------------------------- live meter

void rssiMeter(float mhz) {
  ELECHOUSE_cc1101.setModulation(2);
  ELECHOUSE_cc1101.setRxBW(101.56);
  ELECHOUSE_cc1101.SetRx(mhz);
  delay(5);
  Serial.printf("\nLive signal strength at %.3f MHz, 5 readings/s. Any key stops.\n", mhz);
  while (!stopRequested()) {
    int best = -128;
    uint32_t t0 = millis();
    while (millis() - t0 < 200) {
      int r = ELECHOUSE_cc1101.getRssi();
      if (r > best) best = r;
      delayMicroseconds(300);
    }
    int len = constrain(map(best, -110, -30, 0, 40), 0, 40);
    char bar[42];
    memset(bar, '#', len);
    bar[len] = 0;
    Serial.printf("%4d dBm |%s\n", best, bar);
  }
  ELECHOUSE_cc1101.setSidle();
  Serial.println(F("Stopped."));
}

// ---------------------------------------------------------------- commands

void printHelp() {
  Serial.println(F("scan 433 | 315 | 868 | 915 [seconds]    sweep a preset band"));
  Serial.println(F("scan START END [stepKHz] [seconds]       custom sweep (MHz)"));
  Serial.println(F("listen MHZ [fsk] [-dBm]                  capture bursts (optional trigger)"));
  Serial.println(F("rssi MHZ                                 live signal-strength meter"));
  Serial.println(F("bands   ?    (send any key to stop a scan or listen)"));
}

void handleCommand(String line) {
  line.trim();
  line.toLowerCase();
  if (!line.length()) return;
  int sp = line.indexOf(' ');
  String cmd = sp < 0 ? line : line.substring(0, sp);
  String arg = sp < 0 ? "" : line.substring(sp + 1);

  if (cmd == "scan") {
    float a = 0, b = 0, c = 0, d = 0;
    int n = sscanf(arg.c_str(), "%f %f %f %f", &a, &b, &c, &d);
    for (const Band &bd : kBands) {
      if (n >= 1 && n <= 2 && (int)a == atoi(bd.name)) {
        scan(bd.start, bd.end, 100, n == 2 ? (uint32_t)b : 10);
        return;
      }
    }
    if (n >= 2 && b > a && validFreq(a) && validFreq(b)) {
      scan(a, b, n >= 3 ? c : 100, n >= 4 ? (uint32_t)d : 10);
    } else {
      Serial.println(F("Usage: scan 433 [seconds]  or  scan 430 440 [stepKHz] [seconds]"));
      Serial.println(F("CC1101 range: 300-348, 387-464, 779-928 MHz"));
    }
  } else if (cmd == "listen") {
    float f = atof(arg.c_str());
    if (!validFreq(f)) { Serial.println(F("Usage: listen 433.92 [fsk] [trigger dBm, e.g. -80]")); return; }
    int dash = arg.indexOf(" -");
    int trig = dash >= 0 ? atoi(arg.c_str() + dash + 1) : 0;
    listen(f, arg.indexOf("fsk") >= 0, trig);
  } else if (cmd == "rssi") {
    float f = atof(arg.c_str());
    if (!validFreq(f)) { Serial.println(F("Usage: rssi 433.92")); return; }
    rssiMeter(f);
  } else if (cmd == "bands") {
    for (const Band &bd : kBands) Serial.printf("  %s: %.1f-%.1f MHz\n", bd.name, bd.start, bd.end);
  } else if (cmd == "?" || cmd == "help") {
    printHelp();
  } else {
    Serial.println(F("Unknown command, type ? for help"));
  }
}

void setup() {
  Serial.begin(115200);
  uint32_t t0 = millis();
  while (!Serial && millis() - t0 < 5000) delay(10);

  Serial.println(F("\nCC1101 scanner"));
  while (!radioInit()) {
    Serial.println(F("CC1101 not found. Check wiring (run the PixMob sketch's wiring"));
    Serial.println(F("test) and that SmartRC-CC1101-Driver-Lib is version 2.5.7. Retrying..."));
    delay(5000);
  }
  Serial.println(F("CC1101 ready. Receive only."));
  printHelp();
}

void loop() {
  if (Serial.available()) handleCommand(Serial.readStringUntil('\n'));
}
