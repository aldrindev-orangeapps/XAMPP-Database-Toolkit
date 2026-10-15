CREATE TABLE people (
    id INT PRIMARY KEY,
    email VARCHAR(120),
    note VARCHAR(120)
);
START TRANSACTION;
INSERT INTO people VALUES (1, 'one@example.com', 'semicolon; inside string');
INSERT INTO people VALUES (2, 'two@example.com', 'second row');
COMMIT;
