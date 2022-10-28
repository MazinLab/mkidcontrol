//#include <SPI.h> //one of the arduino library headers

//global variables and hard coded settings
int pin_mirror = 7; // keeping this just to maintain old features, even though it wont be used
int pin808 = 9; // pins 3, 5, 6, 9, 10, and 11 are capable of PWM
//5 and 6 have higher frequency (980 Hz vs 490)
int pin904 = 10;
int pin980 = 11;
int pin1120 = 6;
int pin1310 = 5;
  
int names[] = { //not currently used but will be for sending verification messages
    808, 904, 980, 1120, 1310, pin_mirror
};

int pins[] = { //mappings of index to the physical pin on the arduino
  pin808, pin904, pin980, pin1120, pin1310, pin_mirror
};

unsigned int status[] = {0, 0, 0, 0, 0, 0};

//storing the input bytes
unsigned int pinByte = 0;
unsigned int pwmByte = 0;
  

void print_status(unsigned int *status) {
  for (unsigned int i=0; i<6; i++) {
    Serial.print(i, DEC);
    Serial.print(": ");
    Serial.print(status[i], DEC);
    Serial.print("\n");
  };
}


void setup() {
  Serial.begin(9600); //set up USB serial settings

  pinMode(pin_mirror, OUTPUT);
  digitalWrite(pin_mirror, LOW);
  pinMode(pin808, OUTPUT);
  analogWrite(pin808, 0);
  pinMode(pin904, OUTPUT);
  analogWrite(pin904, 0);
  pinMode(pin980, OUTPUT);
  analogWrite(pin980, 0);
  pinMode(pin1120, OUTPUT);
  analogWrite(pin1120, 0);
  pinMode(pin1310, OUTPUT);
  analogWrite(pin1310, 0);

}


void loop() {
  //Commands are sent as two bytes
  //first is the pin index (index for the pins array
  //second is the pwm value, for the mirror pin, 0 = low , non-zero is high
  if (Serial.available() > 1) {
    pinByte = Serial.read(); // read the pin byte
    pwmByte = Serial.read(); // read the amplitude byte
    if (pinByte == 5) { //this is the one pin still set as digital
      if (pwmByte == 0) {
        digitalWrite(pins[pinByte], LOW);
        Serial.println("digital pin being set low");
        status[5] = 0;
      } else {
        digitalWrite(pins[pinByte], HIGH);
        Serial.println("digital pin being set high");
        status[5] = 1;
      }
    } else if (pinByte >= 6) {
      //If it does not correspond to a pin, instead requests status return
      print_status(status);
    } else { //all other pins are using pwm
        analogWrite(pins[pinByte], pwmByte);
        Serial.println("pwm pin being set");
        status[pinByte] = pwmByte;
    }
  };
  delay(100);
}
