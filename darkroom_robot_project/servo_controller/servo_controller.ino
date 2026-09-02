#include <Servo.h>

// 서보 전용 아두이노 (CH340). 조명 보드(FTDI D7)와 파이 시리얼은 쓰지 않는다.
// PC: "0\n" / "180\n" → "OK <각도>". 원위치(기본)는 0°.
// 주황=D8, 갈=GND, 빨강=5V (가능하면 USB가 아닌 별도 5V)
// 원위치를 18°에서 0°로 내렸다. 펄스 상한이 이미 스톨 직전이라 더 못 넓히는데,
// 시작점을 내리면 펄스를 안 건드리고 회전량을 162°에서 180°로 늘릴 수 있다.
#define SERVO_HOME 0
#define SERVO_PIN 8
// 1000~2000µs는 실제 ~90°. 500~2620은 끝에서 걸릴 수 있어
// 붙인 채로 550~2520을 쓴다 (2500보다 조금 더, 스톨 직전).
#define SERVO_MIN_US 550
#define SERVO_MAX_US 2520
#define SERVO_WAIT_MS 2000

Servo myservo;

// 자동문용 서보 2개. 거치대가 D8이라 문은 D9·D10.
#define DOOR1_PIN 9
#define DOOR2_PIN 10
Servo door1;
Servo door2;

// 자동문 닫힘(정렬) 각도
#define DOOR_CLOSE_ANGLE 90
// 자동문 열림 각도 (서로 반대 방향)
#define DOOR1_OPEN_ANGLE 180
#define DOOR2_OPEN_ANGLE 0

int angleToUs(int angle) {
  return map(angle, 0, 180, SERVO_MIN_US, SERVO_MAX_US);
}

void moveTo(int angle) {
  int us = angleToUs(angle);
  digitalWrite(LED_BUILTIN, HIGH);
  myservo.writeMicroseconds(us);
  delay(SERVO_WAIT_MS);
  digitalWrite(LED_BUILTIN, LOW);
}

void setup() {
  pinMode(LED_BUILTIN, OUTPUT);
  Serial.begin(9600);
  myservo.attach(SERVO_PIN, SERVO_MIN_US, SERVO_MAX_US);
  moveTo(SERVO_HOME);

  door1.attach(DOOR1_PIN);
  door2.attach(DOOR2_PIN);
  door1.write(DOOR1_OPEN_ANGLE);
  door2.write(DOOR2_OPEN_ANGLE);

  Serial.println("READY SERVO D8 DOOR D9/D10");
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

  // 문 개폐 명령어 처리
  if (input == "open") {
    door1.write(DOOR1_OPEN_ANGLE);
    door2.write(DOOR2_OPEN_ANGLE);
    delay(1000); // 문 움직이는 시간 대기
    Serial.println("OK open");
    return;
  }
  if (input == "close") {
    door1.write(DOOR_CLOSE_ANGLE);
    door2.write(DOOR_CLOSE_ANGLE);
    delay(1000);
    Serial.println("OK close");
    return;
  }

  // 숫자면 거치대 각도 명령으로 간주
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
