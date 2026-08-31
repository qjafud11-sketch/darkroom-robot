#include <Servo.h>

// 거치대 서보 (기존)
#define SERVO_HOME 18
#define SERVO_PIN 7
#define SERVO_MIN_US 1000
#define SERVO_MAX_US 2000
#define SERVO_WAIT_MS 1000

Servo myservo;

// 자동문용 서보 2개 (추가)
#define DOOR1_PIN 8
#define DOOR2_PIN 9
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
  myservo.attach(SERVO_PIN, SERVO_MIN_US, SERVO_MAX_US);
  myservo.writeMicroseconds(angleToUs(angle));
  delay(SERVO_WAIT_MS);
}

void setup() {
  Serial.begin(9600);
  
  // 거치대 서보 초기화
  moveTo(SERVO_HOME);
  
  // 문 서보 초기화 (초기 상태: 열림)
  door1.attach(DOOR1_PIN);
  door2.attach(DOOR2_PIN);
  door1.write(DOOR1_OPEN_ANGLE);
  door2.write(DOOR2_OPEN_ANGLE);
  
  Serial.println("READY SERVO 3EA (MOUNT D7, DOOR D8/D9)");
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
