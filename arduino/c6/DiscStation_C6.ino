#include <Wire.h>
#include <Adafruit_GFX.h>
#include <Adafruit_SSD1306.h>
#include <QRCode.h>

#define SCREEN_WIDTH 128
#define SCREEN_HEIGHT 64
#define OLED_RESET    -1
#define I2C_ADDRESS   0x3C

#define BTN_SELECT_PIN  21  // D3
#define BTN_UP_PIN      20  // D9/MISO
#define BTN_DOWN_PIN    18  // D10/MOSI
#define LED_POWER_PIN   15  // Built-in user LED
#define LED_ACT_PIN     16  // D6/TX
#define POT_PIN          0  // D0/A0 (ADC)

#define DEBOUNCE_MS      50
#define LONG_PRESS_MS    1000
#define POT_READ_MS      200
#define DONE_RESET_MS    30000
#define STANDBY_BLANK_MS 60000
#define PING_TIMEOUT_MS  30000
#define LED_BLINK_MS     250

#define MAX_HOME_MODES 5
#define BURN_COUNT 4
#define SPEED_COUNT 4

int homeCount = 3;
String homeModes[MAX_HOME_MODES] = {"BURN", "PLAY", "RIP"};
String discName = "";

Adafruit_SSD1306 display(SCREEN_WIDTH, SCREEN_HEIGHT, &Wire, OLED_RESET);

String ipUrl = "";

enum UiState {
  UI_STATUS,
  UI_HOME,
  UI_BURN_READY,
  UI_PLAY,
  UI_STANDBY,
  UI_WAITING,
  UI_IP,
  UI_DISCONNECTED,
  UI_LOADING
};

const char* BURN_MODES[BURN_COUNT] = {"AUTO", "BEST", "LONG", "TEST"};
const char* SPEED_MODES[SPEED_COUNT] = {"Auto", "6x", "4x", "3x"};

UiState uiState = UI_STATUS;

String titleLine = "";
String metaLine = "";
String fitLine = "";
String discLine = "Disc: none";
String line1 = "Status: READY";
String line2 = "";
String line3 = "";
String waitingLine1 = "";
String waitingLine2 = "";
String loadingLine = "";
int loadingDots = 0;
unsigned long lastLoadingAnim = 0;
int progressPercent = -1;

bool displayOk = false;
unsigned long returnToHomeAt = 0;
unsigned long playStatusAt = 0;
bool playStatusTemp = false;
unsigned long lastPotRead = 0;

unsigned long selectDownAt = 0;
bool selectDown = false;
unsigned long selectLastDebounce = 0;

unsigned long upDownAt = 0;
bool upDown = false;
unsigned long upLastDebounce = 0;

unsigned long downDownAt = 0;
bool downDown = false;
unsigned long downLastDebounce = 0;

int ffSpeedIndex = 0;
int rewSpeedIndex = 0;

bool actLedState = false;
unsigned long lastLedBlink = 0;
int homeIndex = 0;
int burnModeIndex = 0;
int burnSpeedIndex = 0;
bool editingSpeed = false;
int playVolume = 50;
unsigned long standbyStartTime = 0;
unsigned long lastMsgTime = 0;
bool displayBlank = false;
bool audioPlayMode = false;
int displayRotation = 0;

int indeterminateCount = 0;
unsigned long lastIndeterminateAnim = 0;

int readBucket(int count) {
  int raw = analogRead(POT_PIN);
  int idx = raw * count / 4096;
  if (idx < 0) idx = 0;
  if (idx >= count) idx = count - 1;
  return idx;
}

int readPercent() {
  int raw = analogRead(POT_PIN);
  int pct = raw * 100 / 4095;
  if (pct < 0) pct = 0;
  if (pct > 100) pct = 100;
  return ((pct + 2) / 5) * 5;
}

void sendHomeMode() {
  if (homeIndex >= 0 && homeIndex < homeCount) {
    Serial.print("MENU:");
    Serial.println(homeModes[homeIndex]);
  }
}

void sendBurnMode() {
  Serial.print("MODE:");
  Serial.println(BURN_MODES[burnModeIndex]);
}

void printUpper(String value) {
  value.toUpperCase();
  display.print(value);
}

void drawChrome() {
  display.setRotation(displayRotation);
  display.setTextSize(1);
  display.setTextColor(SSD1306_WHITE);
  display.drawLine(0, 0, 4, 0, SSD1306_WHITE);
  display.drawLine(0, 0, 0, 4, SSD1306_WHITE);
  display.drawLine(123, 0, 127, 0, SSD1306_WHITE);
  display.drawLine(127, 0, 127, 4, SSD1306_WHITE);
  display.drawLine(0, 63, 4, 63, SSD1306_WHITE);
  display.drawLine(0, 59, 0, 63, SSD1306_WHITE);
  display.drawLine(123, 63, 127, 63, SSD1306_WHITE);
  display.drawLine(127, 59, 127, 63, SSD1306_WHITE);
}

void drawHeader() {
  display.setTextSize(1);
  display.setTextColor(SSD1306_WHITE);
  drawChrome();
  display.setCursor(4, 0);
  display.print("DISCSTATION // PANEL");
  display.drawLine(6, 10, 121, 10, SSD1306_WHITE);
}

void drawHome() {
  uiState = UI_HOME;
  returnToHomeAt = 0;
  if (!displayOk) return;

  display.clearDisplay();
  drawHeader();

  int y = 14;
  if (discName.length() > 0) {
    display.setTextSize(1);
    display.setCursor(2, y);
    printUpper(discName);
    y += 10;
  } else {
    display.setCursor(2, y);
    printUpper(discLine);
    y += 10;
  }

  for (int i = 0; i < homeCount; i++) {
    display.setCursor(2, y);
    display.print(i == homeIndex ? ">" : " ");
    display.setCursor(14, y);
    printUpper(homeModes[i]);
    y += 10;
  }

  display.display();
}

void drawBurnReady() {
  uiState = UI_BURN_READY;
  returnToHomeAt = 0;
  if (!displayOk) return;

  display.clearDisplay();
  drawHeader();

  display.setCursor(2, 14);
  printUpper(titleLine);

  display.setCursor(2, 25);
  printUpper(metaLine);

  display.setCursor(2, 37);
  printUpper(fitLine);

  display.setCursor(2, 49);
  display.print(editingSpeed ? ' ' : '>');
  display.setCursor(14, 49);
  display.print(BURN_MODES[burnModeIndex]);

  display.setCursor(62, 49);
  display.print(editingSpeed ? '>' : ' ');
  display.setCursor(74, 49);
  display.print(SPEED_MODES[burnSpeedIndex]);

  display.setCursor(2, 57);
  display.print("HOLD SELECT // GO");

  display.display();
}

void drawWaiting() {
  uiState = UI_WAITING;
  returnToHomeAt = 0;
  if (!displayOk) return;

  display.clearDisplay();
  drawHeader();

  display.setCursor(2, 16);
  printUpper(waitingLine1);

  display.setCursor(2, 30);
  printUpper(waitingLine2);

  display.setCursor(2, 49);
  display.print("SHORT // GO");

  display.display();
}

void drawStatus() {
  uiState = UI_STATUS;
  if (!displayOk) return;

  display.clearDisplay();
  drawHeader();

  display.setCursor(2, 16);
  printUpper(line1);
  display.setCursor(2, 30);
  printUpper(line2);
  display.setCursor(2, 44);
  printUpper(line3);

  if (progressPercent >= 0) {
    int barW = map(progressPercent, 0, 100, 0, 124);
    display.drawRect(2, 54, 124, 8, SSD1306_WHITE);
    if (barW > 0) {
      display.fillRect(2, 54, barW, 8, SSD1306_WHITE);
    }
  } else {
    String dots = "";
    for (int i = 0; i < indeterminateCount; i++) dots += ".";
    display.setCursor(2, 56);
    display.print(dots);
  }

  display.display();
}

void drawQrCode(String value) {
  drawChrome();
  QRCode qr;
  uint8_t qrData[qrcode_getBufferSize(2)];
  if (qrcode_initText(&qr, qrData, 2, ECC_LOW, value.c_str()) < 0) {
    display.setCursor(2, 25);
    display.print("QR ERROR // USE URL");
    return;
  }
  const int scale = 2;
  const int quiet = 2;
  const int pixels = qr.size * scale;
  const int left = (SCREEN_WIDTH - pixels) / 2;
  const int top = 2;
  display.fillRect(left - quiet, top - quiet, pixels + quiet * 2, pixels + quiet * 2, SSD1306_WHITE);
  for (uint8_t y = 0; y < qr.size; y++) {
    for (uint8_t x = 0; x < qr.size; x++) {
      if (qrcode_getModule(&qr, x, y)) {
        display.fillRect(left + x * scale, top + y * scale, scale, scale, SSD1306_BLACK);
      }
    }
  }
  display.setCursor(2, 57);
  display.print("SCAN // DISCSTATION");
}

void drawIP() {
  uiState = UI_IP;
  returnToHomeAt = 0;
  if (!displayOk) return;

  display.clearDisplay();
  drawQrCode(ipUrl);

  display.display();
}

void drawStandby() {
  uiState = UI_STANDBY;
  returnToHomeAt = 0;
  standbyStartTime = millis();
  displayBlank = false;
  if (!displayOk) return;

  display.clearDisplay();
  drawHeader();
  display.setCursor(2, 25);
  display.print("STANDBY // READY");
  display.setCursor(2, 40);
  display.print("INSERT MEDIA");
  display.display();
}

void drawDisconnected() {
  uiState = UI_DISCONNECTED;
  returnToHomeAt = 0;
  displayBlank = false;
  if (!displayOk) return;

  display.clearDisplay();
  drawHeader();
  display.setCursor(2, 25);
  display.print("DISCONNECTED // LINK");
  display.setCursor(2, 40);
  display.print("CHECK USB");
  display.display();
}

void drawLoading() {
  uiState = UI_LOADING;
  returnToHomeAt = 0;
  displayBlank = false;
  if (!displayOk) return;

  display.clearDisplay();
  drawHeader();

  display.setTextSize(1);
  display.setCursor(2, 18);
  printUpper(loadingLine);

  String dots = "";
  for (int i = 0; i < loadingDots; i++) dots += ".";
  display.setCursor(2, 32);
  display.print(dots);
  display.setCursor(70, 32);
  display.print("LOADING //");

  display.display();
}

void wakeDisplay() {
  if (!displayBlank || !displayOk) return;
  display.ssd1306_command(0xAF);
  displayBlank = false;
  switch (uiState) {
    case UI_HOME: drawHome(); break;
    case UI_STATUS: drawStatus(); break;
    case UI_IP: drawIP(); break;
    case UI_BURN_READY: drawBurnReady(); break;
    case UI_PLAY: drawPlay(); break;
    case UI_WAITING: drawWaiting(); break;
    case UI_STANDBY: drawStandby(); break;
    case UI_DISCONNECTED: drawDisconnected(); break;
    case UI_LOADING: drawLoading(); break;
  }
}

void drawPlay() {
  uiState = UI_PLAY;
  returnToHomeAt = 0;
  if (!displayOk) return;

  display.clearDisplay();
  drawHeader();

  display.setCursor(2, 16);
  printUpper(line1);
  display.setCursor(2, 30);
  printUpper(line2);
  display.setCursor(2, 44);
  display.print("VOL // ");
  display.print(playVolume);
  display.print("%");
  if (!audioPlayMode) {
    display.setCursor(2, 56);
    display.print("SELECT // PLAY");
  }

  display.display();
}

void parseMessage(String msg) {
  msg.trim();

  if (msg == "PING") {
    lastMsgTime = millis();
    Serial.println("PONG");
    if (uiState == UI_DISCONNECTED) drawStandby();
    return;
  }

  wakeDisplay();
  lastMsgTime = millis();

  Serial.print("RCV:");
  Serial.println(msg);

  if (msg.startsWith("HOME:")) {
    homeIndex = readBucket(homeCount);
    drawHome();

  } else if (msg.startsWith("DISC:")) {
    discLine = msg.substring(5);
    if (discLine.length() > 20) discLine = discLine.substring(0, 20);
    if (uiState == UI_HOME) drawHome();

  } else if (msg.startsWith("DISC_NAME:")) {
    discName = msg.substring(10);
    if (discName.length() > 18) discName = discName.substring(0, 18);
    if (uiState == UI_HOME) drawHome();

  } else if (msg.startsWith("MENU_ITEMS:")) {
    String items = msg.substring(11);
    homeCount = 0;
    while (items.length() > 0 && homeCount < MAX_HOME_MODES) {
      int comma = items.indexOf(',');
      if (comma < 0) {
        homeModes[homeCount++] = items;
        break;
      }
      homeModes[homeCount++] = items.substring(0, comma);
      items = items.substring(comma + 1);
    }
    if (homeIndex >= homeCount) homeIndex = 0;
    if (uiState == UI_HOME) drawHome();

  } else if (msg.startsWith("STANDBY:")) {
    drawStandby();

  } else if (msg.startsWith("TITLE:")) {
    titleLine = msg.substring(6);
    if (titleLine.length() > 20) titleLine = titleLine.substring(0, 20);
    burnModeIndex = readBucket(BURN_COUNT);
    drawBurnReady();

  } else if (msg.startsWith("META:")) {
    metaLine = msg.substring(5);
    if (metaLine.length() > 20) metaLine = metaLine.substring(0, 20);
    drawBurnReady();

  } else if (msg.startsWith("FIT:")) {
    fitLine = msg.substring(4);
    if (fitLine.length() > 20) fitLine = fitLine.substring(0, 20);
    drawBurnReady();

  } else if (msg.startsWith("PLAY:")) {
    line1 = msg.substring(5);
    line2 = "Playing disc";
    playVolume = readPercent();
    drawPlay();

  } else if (msg.startsWith("PLAY_MODE:")) {
    audioPlayMode = msg.substring(10) == "AUDIO_CD";
    drawPlay();

  } else if (msg.startsWith("PLAY_STATUS:")) {
    line1 = msg.substring(12);
    if (line1.length() > 20) line1 = line1.substring(0, 20);
    if (line1 == "PLAYING" || line1 == "PAUSED" || (audioPlayMode && line1.startsWith("TRACK "))) {
      playStatusTemp = false;
    } else {
      playStatusAt = millis();
      playStatusTemp = true;
    }
    drawPlay();

  } else if (msg.startsWith("WAITING:")) {
    returnToHomeAt = 0;
    String rest = msg.substring(8);
    int slash = rest.indexOf('/');
    if (slash >= 0) {
      waitingLine1 = "Data: " + rest.substring(0, slash);
      waitingLine1.trim();
      waitingLine2 = "Disc: " + rest.substring(slash + 1);
      waitingLine2.trim();
    } else {
      waitingLine1 = rest;
      waitingLine2 = "";
    }
    drawWaiting();

  } else if (msg.startsWith("STATUS:")) {
    returnToHomeAt = 0;
    line1 = msg.substring(7);
    line2 = "";
    line3 = "";
    // Don't reset progressPercent here - a STATUS: label change (e.g.
    // "Copying files..." -> "Converting...") is often immediately
    // followed by a fresh PROGRESS: for the same ongoing operation.
    // Wiping it every time flipped the bar to the indeterminate dots
    // and back on every single label update, flickering between the
    // two. DONE:/CANCELLED:/ERROR:/NO_DISC: still reset it below - those
    // really are the end of an operation.
    drawStatus();

  } else if (msg.startsWith("PROGRESS:")) {
    returnToHomeAt = 0;
    line2 = msg.substring(9);
    {
      String rest = msg.substring(9);
      int pct = rest.indexOf('%');
      if (pct > 0) {
        String num = rest.substring(0, pct);
        num.trim();
        int val = num.toInt();
        if (val >= 0 && val <= 100) {
          progressPercent = val;
        }
      }
    }
    drawStatus();

  } else if (msg.startsWith("INFO:")) {
    returnToHomeAt = 0;
    line3 = msg.substring(5);
    drawStatus();

  } else if (msg.startsWith("DONE:")) {
    line1 = "DONE!";
    line2 = msg.substring(5);
    line3 = "Returning home";
    returnToHomeAt = millis() + DONE_RESET_MS;
    progressPercent = -1;
    drawStatus();

  } else if (msg.startsWith("CANCELLED:")) {
    returnToHomeAt = 0;
    line1 = "CANCELLED";
    line2 = msg.substring(10);
    line3 = "Use menu";
    progressPercent = -1;
    drawStatus();

  } else if (msg.startsWith("WARNING:")) {
    returnToHomeAt = 0;
    line1 = "!! WARNING !!";
    line2 = msg.substring(8);
    if (line2.length() > 20) line2 = line2.substring(0, 20);
    line3 = "";
    drawStatus();

  } else if (msg.startsWith("ERROR:")) {
    returnToHomeAt = 0;
    line1 = "!! ERROR";
    line2 = msg.substring(6);
    line3 = "";
    progressPercent = -1;
    drawStatus();

  } else if (msg.startsWith("IP:")) {
    ipUrl = msg.substring(3);
    drawIP();

  } else if (msg.startsWith("LOADING:")) {
    loadingLine = msg.substring(8);
    loadingDots = 0;
    lastLoadingAnim = millis();
    drawLoading();

  } else if (msg.startsWith("NO_DISC:")) {
    line1 = msg.substring(8);
    line2 = "";
    line3 = "";
    progressPercent = -1;
    drawStatus();
  }
}

void setup() {
  Serial.begin(115200);
  Wire.begin(22, 23);

  pinMode(3, OUTPUT);
  digitalWrite(3, LOW);
  pinMode(14, OUTPUT);
  digitalWrite(14, LOW);

  pinMode(BTN_SELECT_PIN, INPUT_PULLUP);
  pinMode(BTN_UP_PIN, INPUT_PULLUP);
  pinMode(BTN_DOWN_PIN, INPUT_PULLUP);
  pinMode(LED_POWER_PIN, OUTPUT);
  pinMode(LED_ACT_PIN, OUTPUT);
  digitalWrite(LED_POWER_PIN, HIGH);
  digitalWrite(LED_ACT_PIN, LOW);
  pinMode(POT_PIN, INPUT);
  analogReadResolution(12);

  homeIndex = readBucket(homeCount);
  burnModeIndex = readBucket(BURN_COUNT);
  playVolume = readPercent();

  displayOk = display.begin(SSD1306_SWITCHCAPVCC, I2C_ADDRESS);

  if (displayOk) {
    Serial.println("Display OK");
  } else {
    Serial.println("Display FAILED");
    delay(3000);
  }

  for (int i = 0; i < 3; i++) {
    digitalWrite(LED_ACT_PIN, HIGH);
    delay(150);
    digitalWrite(LED_ACT_PIN, LOW);
    delay(150);
  }

  drawStandby();
  lastMsgTime = millis();
  Serial.println("DISCSTATION_READY");
}

void handleSelectPress(bool longPress) {
  wakeDisplay();
  if (uiState == UI_HOME) {
    if (longPress) {
      Serial.println("EJECT");
      line1 = "Ejecting...";
      line2 = "";
      line3 = "";
      returnToHomeAt = millis() + 5000;
      drawStatus();
    } else if (homeIndex >= 0 && homeIndex < homeCount) {
      Serial.print("SELECT:");
      Serial.println(homeModes[homeIndex]);
      line1 = "Selected";
      line2 = homeModes[homeIndex];
      line3 = "";
      drawStatus();
    }

  } else if (uiState == UI_STANDBY) {
    Serial.println("EJECT");
    line1 = "Ejecting...";
    line2 = "";
    line3 = "";
    returnToHomeAt = millis() + 5000;
    drawStatus();

  } else if (uiState == UI_STATUS && line1 == "Ejecting...") {
    drawStandby();

  } else if (uiState == UI_IP) {
    if (longPress) {
      Serial.println("CANCEL");
      drawHome();
    }

  } else if (uiState == UI_WAITING) {
    if (longPress) {
      Serial.println("CANCEL");
      drawHome();
    } else {
      Serial.println("CONFIRM");
      line1 = "Confirmed!";
      line2 = "";
      line3 = "";
      drawStatus();
    }

  } else if (uiState == UI_BURN_READY) {
    if (longPress) {
      Serial.print("START:");
      Serial.println(BURN_MODES[burnModeIndex]);
      line1 = "Starting...";
      line2 = BURN_MODES[burnModeIndex];
      line3 = "";
      drawStatus();
    } else {
      editingSpeed = !editingSpeed;
      drawBurnReady();
    }

  } else if (uiState == UI_STATUS) {
    if (longPress) {
      Serial.println("CANCEL");
      line1 = "Cancelling...";
      line2 = "";
      line3 = "";
      drawStatus();
    }

  } else if (uiState == UI_DISCONNECTED) {
    Serial.println("CANCEL");
    drawStandby();

  } else if (uiState == UI_PLAY) {
    ffSpeedIndex = 0;
    rewSpeedIndex = 0;
    if (longPress) {
      Serial.println("PLAY_STOP");
    } else {
      Serial.println("PLAY_BUTTON");
    }
  }
}

void handleUp(bool longPress) {
  wakeDisplay();
  if (uiState == UI_HOME) {
    if (homeCount > 0) {
      homeIndex = (homeIndex - 1 + homeCount) % homeCount;
      sendHomeMode();
      drawHome();
    }
  } else if (uiState == UI_BURN_READY) {
    if (editingSpeed) {
      burnSpeedIndex = (burnSpeedIndex - 1 + SPEED_COUNT) % SPEED_COUNT;
      Serial.print("SPEED:");
      Serial.println(SPEED_MODES[burnSpeedIndex]);
      drawBurnReady();
    } else {
      burnModeIndex = (burnModeIndex - 1 + BURN_COUNT) % BURN_COUNT;
      sendBurnMode();
      drawBurnReady();
    }
  } else if (uiState == UI_PLAY) {
    if (audioPlayMode) {
      Serial.println(displayRotation == 0 ? (longPress ? "REW:BIG" : "REW:10") : (longPress ? "FF:BIG" : "FF:10"));
      return;
    }
    rewSpeedIndex = 0;
    if (longPress) {
      Serial.println("FF:BIG");
    } else {
      ffSpeedIndex = (ffSpeedIndex + 1) % 4;
      int seek_sec[] = {10, 20, 30, 60};
      Serial.print("FF:");
      Serial.println(seek_sec[ffSpeedIndex]);
    }
  }
}

void handleDown(bool longPress) {
  wakeDisplay();
  if (uiState == UI_HOME) {
    if (homeCount > 0) {
      homeIndex = (homeIndex + 1) % homeCount;
      sendHomeMode();
      drawHome();
    }
  } else if (uiState == UI_BURN_READY) {
    if (editingSpeed) {
      burnSpeedIndex = (burnSpeedIndex + 1) % SPEED_COUNT;
      Serial.print("SPEED:");
      Serial.println(SPEED_MODES[burnSpeedIndex]);
      drawBurnReady();
    } else {
      burnModeIndex = (burnModeIndex + 1) % BURN_COUNT;
      sendBurnMode();
      drawBurnReady();
    }
  } else if (uiState == UI_PLAY) {
    if (audioPlayMode) {
      Serial.println(displayRotation == 0 ? (longPress ? "FF:BIG" : "FF:10") : (longPress ? "REW:BIG" : "REW:10"));
      return;
    }
    ffSpeedIndex = 0;
    if (longPress) {
      Serial.println("REW:BIG");
    } else {
      rewSpeedIndex = (rewSpeedIndex + 1) % 4;
      int seek_sec[] = {10, 20, 30, 60};
      Serial.print("REW:");
      Serial.println(seek_sec[rewSpeedIndex]);
    }
  }
}

void loop() {
  if (Serial.available()) {
    String msg = Serial.readStringUntil('\n');
    parseMessage(msg);
  }

  if (returnToHomeAt != 0 && (long)(millis() - returnToHomeAt) >= 0) {
    drawHome();
  }

  if (playStatusTemp && (long)(millis() - playStatusAt) >= 2500) {
    playStatusTemp = false;
    line1 = "PLAYING";
    drawPlay();
  }

  if (uiState == UI_PLAY && millis() - lastPotRead > POT_READ_MS) {
    lastPotRead = millis();
    int nextVolume = readPercent();
    if (abs(nextVolume - playVolume) >= 3) {
      playVolume = nextVolume;
      Serial.print("POT:");
      Serial.println(playVolume);
      drawPlay();
    }
  }

  {
    bool sel = digitalRead(BTN_SELECT_PIN) == LOW;
    if (sel && !selectDown && millis() - selectLastDebounce > DEBOUNCE_MS) {
      selectDown = true;
      selectDownAt = millis();
    }
    if (!sel && selectDown) {
      selectDown = false;
      selectLastDebounce = millis();
      handleSelectPress(millis() - selectDownAt >= LONG_PRESS_MS);
    }
  }

  {
    bool up = digitalRead(BTN_UP_PIN) == LOW;
    if (up && !upDown && millis() - upLastDebounce > DEBOUNCE_MS) {
      upDown = true;
      upDownAt = millis();
    }
    if (!up && upDown) {
      upDown = false;
      upLastDebounce = millis();
      bool lp = millis() - upDownAt >= LONG_PRESS_MS;
      if (uiState == UI_PLAY || !lp) {
        handleUp(lp);
      }
    }
  }

  {
    bool dn = digitalRead(BTN_DOWN_PIN) == LOW;
    if (dn && !downDown && millis() - downLastDebounce > DEBOUNCE_MS) {
      downDown = true;
      downDownAt = millis();
    }
    if (!dn && downDown) {
      downDown = false;
      downLastDebounce = millis();
      bool lp = millis() - downDownAt >= LONG_PRESS_MS;
      if (uiState == UI_PLAY || !lp) {
        handleDown(lp);
      }
    }
  }

  if (uiState == UI_STATUS && progressPercent < 0 &&
      millis() - lastIndeterminateAnim > 500) {
    lastIndeterminateAnim = millis();
    indeterminateCount = (indeterminateCount % 4) + 1;
    if (!displayBlank) drawStatus();
  }

  if (uiState == UI_LOADING && millis() - lastLoadingAnim > 400) {
    lastLoadingAnim = millis();
    loadingDots = (loadingDots % 4) + 1;
    if (!displayBlank) drawLoading();
  }

  if (displayOk && uiState == UI_STANDBY && !displayBlank &&
      (long)(millis() - standbyStartTime) >= STANDBY_BLANK_MS) {
    display.ssd1306_command(0xAE);
    displayBlank = true;
    digitalWrite(LED_POWER_PIN, LOW);
  }
  if (!displayBlank) {
    digitalWrite(LED_POWER_PIN, HIGH);
  }

  if (uiState != UI_DISCONNECTED &&
      (long)(millis() - lastMsgTime) >= PING_TIMEOUT_MS) {
    drawDisconnected();
  }

  {
    bool ledOn = false;
    if (uiState == UI_BURN_READY || uiState == UI_PLAY || uiState == UI_WAITING || uiState == UI_IP) {
      ledOn = true;
    } else if (uiState == UI_STATUS) {
      if (progressPercent >= 0) {
        ledOn = (millis() - lastLedBlink > LED_BLINK_MS);
        if (ledOn) {
          lastLedBlink = millis();
          actLedState = !actLedState;
        }
        ledOn = actLedState;
      } else {
        ledOn = true;
      }
    } else if (uiState == UI_HOME || uiState == UI_STANDBY || uiState == UI_LOADING) {
      ledOn = false;
    } else if (uiState == UI_DISCONNECTED) {
      ledOn = true;
    }
    digitalWrite(LED_ACT_PIN, ledOn ? HIGH : LOW);
  }

  delay(20);
}
