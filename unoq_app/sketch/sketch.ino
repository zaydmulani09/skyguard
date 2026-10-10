// SkyGuard LED matrix (runs on the UNO Q's STM32U585 MCU).
// Linux side calls Bridge "matrix" with mode: 0 = idle sweep (clear), 1 = flashing warning icon (DRONE), 2 = possible.
#include <Arduino_RouterBridge.h>
#include <Arduino_LED_Matrix.h>

Arduino_LED_Matrix matrix;
volatile int mode = 0;

// 13 columns x 8 rows, 1 = on.  Warning triangle with "!".
static const uint8_t WARN[8][13] = {
  {0,0,0,0,0,0,1,0,0,0,0,0,0},
  {0,0,0,0,0,1,1,1,0,0,0,0,0},
  {0,0,0,0,0,1,1,1,0,0,0,0,0},
  {0,0,0,0,1,1,0,1,1,0,0,0,0},
  {0,0,0,0,1,1,0,1,1,0,0,0,0},
  {0,0,0,1,1,1,1,1,1,1,0,0,0},
  {0,0,0,0,0,0,0,0,0,0,0,0,0},
  {0,0,0,0,1,1,0,1,1,0,0,0,0},
};

void set_mode(int m) { mode = m; }   // do NOT call Bridge.* inside a provided function (deadlock risk)

void setup() {
  matrix.begin();
  Bridge.begin();
  Bridge.provide("matrix", set_mode);
}

void render(const uint8_t f[8][13]) {
  uint8_t buf[104];
  for (int r = 0; r < 8; r++) for (int c = 0; c < 13; c++) buf[r * 13 + c] = f[r][c] ? 1 : 0;
  matrix.draw(buf);
}

void loop() {
  static uint8_t f[8][13];
  uint32_t t = millis();
  memset(f, 0, sizeof(f));
  if (mode == 1) {                         // DRONE: flash the warning icon at ~3 Hz
    if ((t / 160) % 2 == 0) memcpy(f, WARN, sizeof(f));
  } else if (mode == 2) {                  // POSSIBLE: fast scanning bar
    int c = (t / 90) % 13; for (int r = 0; r < 8; r++) f[r][c] = 1;
  } else {                                 // CLEAR: slow radar sweep dot-line
    int c = (t / 220) % 13; f[3][c] = 1; f[4][c] = 1;
  }
  render(f);
  delay(30);
}
