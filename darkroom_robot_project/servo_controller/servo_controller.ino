#include <Servo.h>

// 서보 전용 아두이노 (CH340). 조명 보드(FTDI D8)와 파이 시리얼은 쓰지 않는다.
// PC: "18\n" / "180\n" → "OK <각도>". 원위치(기본)는 18°.
// 주황=D7, 갈=GND, 빨강=5V (가능하면 USB가 아닌 별도 5V)
#define SERVO_HOME 18
#define SERVO_PIN 7
#define SERVO_MIN_US 1000
#define SERVO_MAX_US 2000
#define SERVO_WAIT_MS 1000

Servo myservo;

int angleToUs(int angle) {
  return map(angle, 0, 180, SERVO_MIN_US, SERVO_MAX_US);
}

void moveTo(int angle) {
  myservo.attach(SERVO_PIN, SERVO_MIN_US, SERVO_MAX_US);
  myservo.writeMicroseconds(angleToUs(angle));
  delay(SERVO_WAIT_MS);
}

void setup() {
  Serial.begin(9600);
  moveTo(SERVO_HOME);
  Serial.println("READY SERVO D7");
}

void loop() {
  if (Serial.available() <= 0) {
    return;
  }

  String input = Serial.readStringUntil('\n');
  input.trim();
  if (input.length() == 0) {
    return;
  }

  bool digits = true;
  for (unsigned int i = 0; i < input.length(); i++) {
    if (input.charAt(i) < '0' || input.charAt(i) > '9') {
      digits = false;
      break;
    }
  }
  if (!digits) {
    Serial.println("ERR CMD");
    return;
  }

  int angle = input.toInt();
  if (angle < 0 || angle > 180) {
    Serial.println("ERR ANGLE");
    return;
  }

  moveTo(angle);
  Serial.print("OK ");
  Serial.println(angle);
}
