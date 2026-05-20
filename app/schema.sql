-- =====================================================================
-- schema.sql -- Logistics-Prod schema for RDS PostgreSQL
-- Extends the Logistics (ASG+RDS) schema with:
--   * proof_photo_key column on shipments (S3 object key for delivery photo)
-- Run via SSM Run Command after the RDS instance is "available":
--     psql -h <rds-endpoint> -U appadmin -d logistics -f schema.sql
-- =====================================================================

-- Drop in reverse-dependency order so the script is re-runnable.
DROP TABLE IF EXISTS shipments CASCADE;
DROP TABLE IF EXISTS drivers   CASCADE;
DROP TABLE IF EXISTS customers CASCADE;

-- ---------------------------------------------------------------------
-- customers : the people who place shipping orders
-- ---------------------------------------------------------------------
CREATE TABLE customers (
    customer_id  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    full_name    VARCHAR(120) NOT NULL,
    email        VARCHAR(255) NOT NULL UNIQUE,
    phone        VARCHAR(30),
    city         VARCHAR(80),
    created_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

-- ---------------------------------------------------------------------
-- drivers : the people who deliver shipments
-- ---------------------------------------------------------------------
CREATE TABLE drivers (
    driver_id       BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    full_name       VARCHAR(120) NOT NULL,
    license_number  VARCHAR(40)  NOT NULL UNIQUE,
    phone           VARCHAR(30),
    vehicle_plate   VARCHAR(20),
    hired_at        DATE         NOT NULL DEFAULT CURRENT_DATE
);

-- ---------------------------------------------------------------------
-- shipments : the core table linking customers and drivers.
--
-- Logistics-Prod additions vs. the original Logistics schema:
--   * proof_photo_key -- S3 object key for the delivery proof photo.
--     Format: shipments/{shipment_id}/{uuid}.jpg
--     NULL until the driver uploads a photo.
--     The full URL is constructed by the app as:
--       <cloudfront-domain>/media/<proof_photo_key>
-- ---------------------------------------------------------------------
CREATE TABLE shipments (
    shipment_id      BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tracking_number  VARCHAR(30)  NOT NULL UNIQUE,
    customer_id      BIGINT       NOT NULL
                     REFERENCES customers(customer_id) ON DELETE RESTRICT,
    driver_id        BIGINT
                     REFERENCES drivers(driver_id)   ON DELETE SET NULL,
    origin           VARCHAR(120) NOT NULL,
    destination      VARCHAR(120) NOT NULL,
    weight_kg        NUMERIC(8,2),
    status           VARCHAR(20)  NOT NULL
                     CHECK (status IN ('pending','in_transit','delivered','delayed','canceled')),
    created_at       TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    delivered_at     TIMESTAMPTZ,
    proof_photo_key  TEXT
);

CREATE INDEX idx_shipments_customer ON shipments(customer_id);
CREATE INDEX idx_shipments_driver   ON shipments(driver_id);
CREATE INDEX idx_shipments_status   ON shipments(status);
CREATE INDEX idx_shipments_tracking ON shipments(tracking_number);

-- =====================================================================
-- Seed data (10 customers, 10 drivers, 10 shipments)
-- Mix of statuses and relationships -- designed to show students:
--   * customer #1 has 3 shipments                   (one-to-many)
--   * driver #2 has 3 shipments                     (one-to-many)
--   * TRK-100001 and TRK-100007 have proof photos   (S3 key set)
--   * all five statuses appear                      (CHECK constraint)
-- =====================================================================

INSERT INTO customers (full_name, email, phone, city) VALUES
  ('Aaron Cohen',     'aaron@example.com',   '+972-50-1234567', 'Tel Aviv'),
  ('Layla Hassan',    'layla@example.com',   '+971-55-9876543', 'Dubai'),
  ('Maria Garcia',    'maria@example.com',   '+34-911-223344',  'Madrid'),
  ('James Patel',     'james@example.com',   '+44-20-7946-0123','London'),
  ('Yuki Tanaka',     'yuki@example.com',    '+81-3-1234-5678', 'Tokyo'),
  ('Ahmad Khalil',    'ahmad@example.com',   '+962-6-555-1234', 'Amman'),
  ('Sara Levi',       'sara@example.com',    '+972-3-987-6543', 'Haifa'),
  ('David Smith',     'david@example.com',   '+1-212-555-0143', 'New York'),
  ('Fatima Al-Saud',  'fatima@example.com',  '+966-11-234-5678','Riyadh'),
  ('Hannah Mueller',  'hannah@example.com',  '+49-30-12345678', 'Berlin');

INSERT INTO drivers (full_name, license_number, phone, vehicle_plate, hired_at) VALUES
  ('Omar Said',       'DL-2391', '+972-52-1112222', '12-345-67', '2022-03-15'),
  ('Yossi Cohen',     'DL-3120', '+972-54-3334444', '23-456-78', '2021-07-01'),
  ('Rashid Hamdan',   'DL-9901', '+971-50-9998888', 'DXB-1234',  '2023-01-20'),
  ('Pierre Dubois',   'DL-7765', '+33-1-2345-6789', 'AB-123-CD', '2020-11-10'),
  ('Mike Johnson',    'DL-4422', '+1-303-555-0199', 'CO-7891',   '2022-08-05'),
  ('Carlos Mendez',   'DL-5566', '+34-91-555-3322', 'MAD-4422',  '2024-02-14'),
  ('Hiroshi Sato',    'DL-8801', '+81-90-1234-5678','TYO-9988',  '2019-05-30'),
  ('Anwar Mahmoud',   'DL-3344', '+966-50-111-2233','RUH-5566',  '2023-09-12'),
  ('Stefan Weber',    'DL-6677', '+49-30-9988-7766','B-MW-1234', '2021-12-01'),
  ('Tom Lee',         'DL-2255', '+44-7700-900123', 'LD-1122-AB','2022-06-18');

INSERT INTO shipments
  (tracking_number, customer_id, driver_id, origin, destination,
   weight_kg, status, delivered_at, proof_photo_key)
VALUES
  ('TRK-100001', 1,  2,  'Tel Aviv',  'Haifa',      12.50, 'delivered',
   NOW() - INTERVAL '5 days',
   'shipments/1/sample-proof-tel-aviv-haifa.jpg'),
  ('TRK-100002', 3,  6,  'Madrid',    'Barcelona',   5.20, 'in_transit',
   NULL, NULL),
  ('TRK-100003', 2,  3,  'Dubai',     'Abu Dhabi',  22.00, 'delivered',
   NOW() - INTERVAL '3 days', NULL),
  ('TRK-100004', 4,  10, 'London',    'Manchester',  8.70, 'canceled',
   NULL, NULL),
  ('TRK-100005', 1,  1,  'Tel Aviv',  'Eilat',       3.40, 'delivered',
   NOW() - INTERVAL '10 days', NULL),
  ('TRK-100006', 5,  7,  'Tokyo',     'Osaka',      15.00, 'delayed',
   NULL, NULL),
  ('TRK-100007', 7,  2,  'Haifa',     'Jerusalem',   2.80, 'delivered',
   NOW() - INTERVAL '1 day',
   'shipments/7/sample-proof-haifa-jerusalem.jpg'),
  ('TRK-100008', 9,  8,  'Riyadh',    'Jeddah',     30.00, 'delivered',
   NOW() - INTERVAL '7 days', NULL),
  ('TRK-100009', 6,  1,  'Amman',     'Aqaba',       7.50, 'canceled',
   NULL, NULL),
  ('TRK-100010', 1,  2,  'Tel Aviv',  'Beersheba',   4.00, 'pending',
   NULL, NULL);

-- Quick sanity check
SELECT
  (SELECT COUNT(*) FROM customers) AS customers,
  (SELECT COUNT(*) FROM drivers)   AS drivers,
  (SELECT COUNT(*) FROM shipments) AS shipments,
  (SELECT COUNT(*) FROM shipments WHERE proof_photo_key IS NOT NULL) AS with_photo;
