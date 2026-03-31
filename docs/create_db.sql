CREATE DATABASE IF NOT EXISTS endurance_planner
  CHARACTER SET utf8mb4
  COLLATE utf8mb4_unicode_ci;

CREATE USER IF NOT EXISTS 'endurance_user'@'localhost'
  IDENTIFIED BY 'localdevpassword';

GRANT ALL PRIVILEGES ON endurance_planner.*
  TO 'endurance_user'@'localhost';

FLUSH PRIVILEGES;
