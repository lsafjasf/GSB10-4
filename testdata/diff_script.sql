-- generated differential script; semicolon; in header comment
CREATE TABLE accounts(id INTEGER PRIMARY KEY, name TEXT, balance INTEGER);
/* block ; comment with ; multiple */ CREATE TABLE log(id INTEGER PRIMARY KEY, msg TEXT);
INSERT OR REPLACE INTO accounts VALUES(0, 'al;ice', 1824);
-- log; the 0-th row
INSERT OR REPLACE INTO log VALUES(0, 'step;0;done');
/* audit; checkpoint; row 0 */
INSERT OR REPLACE INTO accounts VALUES(1, 'bo;b;;o', 409);
INSERT OR REPLACE INTO accounts VALUES(2, 'car''ol;o', 4506);
INSERT OR REPLACE INTO accounts VALUES(3, 'di;no''x', 4012);
INSERT OR REPLACE INTO accounts VALUES(4, 'eve;y;thing', 3657);
INSERT OR REPLACE INTO accounts VALUES(5, 'frank;z', 2286);
INSERT OR REPLACE INTO accounts VALUES(6, 'gr;ace', 1679);
INSERT OR REPLACE INTO accounts VALUES(7, 'h;e;idi', 8935);
-- log; the 7-th row
INSERT OR REPLACE INTO log VALUES(7, 'step;7;done');
INSERT OR REPLACE INTO accounts VALUES(8, 'iva;n''p', 1424);
INSERT OR REPLACE INTO accounts VALUES(9, 'jud;y', 9674);
INSERT OR REPLACE INTO accounts VALUES(10, 'al;ice', 6912);
INSERT OR REPLACE INTO accounts VALUES(11, 'bo;b;;o', 520);
INSERT OR REPLACE INTO accounts VALUES(12, 'car''ol;o', 488);
INSERT OR REPLACE INTO accounts VALUES(13, 'di;no''x', 1535);
/* audit; checkpoint; row 13 */
INSERT OR REPLACE INTO accounts VALUES(14, 'eve;y;thing', 3582);
-- log; the 14-th row
INSERT OR REPLACE INTO log VALUES(14, 'step;14;done');
INSERT OR REPLACE INTO accounts VALUES(15, 'frank;z', 3811);
INSERT OR REPLACE INTO accounts VALUES(16, 'gr;ace', 8279);
INSERT OR REPLACE INTO accounts VALUES(17, 'h;e;idi', 9863);
INSERT OR REPLACE INTO accounts VALUES(18, 'iva;n''p', 434);
INSERT OR REPLACE INTO accounts VALUES(19, 'jud;y', 9195);
INSERT OR REPLACE INTO accounts VALUES(20, 'al;ice', 3257);
INSERT OR REPLACE INTO accounts VALUES(21, 'bo;b;;o', 8928);
-- log; the 21-th row
INSERT OR REPLACE INTO log VALUES(21, 'step;21;done');
INSERT OR REPLACE INTO accounts VALUES(22, 'car''ol;o', 6873);
INSERT OR REPLACE INTO accounts VALUES(23, 'di;no''x', 3611);
INSERT OR REPLACE INTO accounts VALUES(24, 'eve;y;thing', 7359);
INSERT OR REPLACE INTO accounts VALUES(25, 'frank;z', 9654);
INSERT OR REPLACE INTO accounts VALUES(26, 'gr;ace', 4557);
/* audit; checkpoint; row 26 */
INSERT OR REPLACE INTO accounts VALUES(27, 'h;e;idi', 106);
INSERT OR REPLACE INTO accounts VALUES(28, 'iva;n''p', 2615);
-- log; the 28-th row
INSERT OR REPLACE INTO log VALUES(28, 'step;28;done');
INSERT OR REPLACE INTO accounts VALUES(29, 'jud;y', 6924);
INSERT OR REPLACE INTO accounts VALUES(30, 'al;ice', 5574);
INSERT OR REPLACE INTO accounts VALUES(31, 'bo;b;;o', 4552);
INSERT OR REPLACE INTO accounts VALUES(32, 'car''ol;o', 2547);
INSERT OR REPLACE INTO accounts VALUES(33, 'di;no''x', 3527);
INSERT OR REPLACE INTO accounts VALUES(34, 'eve;y;thing', 5514);
INSERT OR REPLACE INTO accounts VALUES(35, 'frank;z', 1674);
-- log; the 35-th row
INSERT OR REPLACE INTO log VALUES(35, 'step;35;done');
INSERT OR REPLACE INTO accounts VALUES(36, 'gr;ace', 1519);
INSERT OR REPLACE INTO accounts VALUES(37, 'h;e;idi', 6224);
INSERT OR REPLACE INTO accounts VALUES(38, 'iva;n''p', 1584);
INSERT OR REPLACE INTO accounts VALUES(39, 'jud;y', 5881);
/* audit; checkpoint; row 39 */
INSERT OR REPLACE INTO accounts VALUES(40, 'al;ice', 5635);
INSERT OR REPLACE INTO accounts VALUES(41, 'bo;b;;o', 9891);
INSERT OR REPLACE INTO accounts VALUES(42, 'car''ol;o', 4333);
-- log; the 42-th row
INSERT OR REPLACE INTO log VALUES(42, 'step;42;done');
INSERT OR REPLACE INTO accounts VALUES(43, 'di;no''x', 711);
INSERT OR REPLACE INTO accounts VALUES(44, 'eve;y;thing', 7527);
INSERT OR REPLACE INTO accounts VALUES(45, 'frank;z', 8785);
INSERT OR REPLACE INTO accounts VALUES(46, 'gr;ace', 2045);
INSERT OR REPLACE INTO accounts VALUES(47, 'h;e;idi', 6201);
INSERT OR REPLACE INTO accounts VALUES(48, 'iva;n''p', 1291);
INSERT OR REPLACE INTO accounts VALUES(49, 'jud;y', 9044);
-- log; the 49-th row
INSERT OR REPLACE INTO log VALUES(49, 'step;49;done');
INSERT OR REPLACE INTO accounts VALUES(50, 'al;ice', 4803);
INSERT OR REPLACE INTO accounts VALUES(51, 'bo;b;;o', 5925);
INSERT OR REPLACE INTO accounts VALUES(52, 'car''ol;o', 9459);
/* audit; checkpoint; row 52 */
INSERT OR REPLACE INTO accounts VALUES(53, 'di;no''x', 3150);
INSERT OR REPLACE INTO accounts VALUES(54, 'eve;y;thing', 1139);
INSERT OR REPLACE INTO accounts VALUES(55, 'frank;z', 750);
INSERT OR REPLACE INTO accounts VALUES(56, 'gr;ace', 3733);
-- log; the 56-th row
INSERT OR REPLACE INTO log VALUES(56, 'step;56;done');
INSERT OR REPLACE INTO accounts VALUES(57, 'h;e;idi', 4741);
INSERT OR REPLACE INTO accounts VALUES(58, 'iva;n''p', 1307);
INSERT OR REPLACE INTO accounts VALUES(59, 'jud;y', 3814);
DELIMITER $$
CREATE TRIGGER accounts_ai AFTER INSERT ON accounts BEGIN
  INSERT OR REPLACE INTO log VALUES(NEW.id, 'trigger;' || NEW.name || ';end');
END$$
DELIMITER ;
INSERT OR REPLACE INTO accounts VALUES(60, 'post60;name', 1654);
INSERT OR REPLACE INTO accounts VALUES(61, 'post61;name', 6227);
INSERT OR REPLACE INTO accounts VALUES(62, 'post62;name', 4554);
INSERT OR REPLACE INTO accounts VALUES(63, 'post63;name', 7428);
INSERT OR REPLACE INTO accounts VALUES(64, 'post64;name', 5977);
INSERT OR REPLACE INTO accounts VALUES(65, 'post65;name', 2664);
INSERT OR REPLACE INTO accounts VALUES(66, 'post66;name', 6065);
INSERT OR REPLACE INTO accounts VALUES(67, 'post67;name', 5820);
INSERT OR REPLACE INTO accounts VALUES(68, 'post68;name', 3432);
INSERT OR REPLACE INTO accounts VALUES(69, 'post69;name', 4374);
INSERT OR REPLACE INTO accounts VALUES(70, 'post70;name', 1169);
INSERT OR REPLACE INTO accounts VALUES(71, 'post71;name', 9980);
INSERT OR REPLACE INTO accounts VALUES(72, 'post72;name', 2803);
INSERT OR REPLACE INTO accounts VALUES(73, 'post73;name', 8751);
INSERT OR REPLACE INTO accounts VALUES(74, 'post74;name', 4010);
INSERT OR REPLACE INTO accounts VALUES(75, 'post75;name', 2677);
INSERT OR REPLACE INTO accounts VALUES(76, 'post76;name', 7573);
INSERT OR REPLACE INTO accounts VALUES(77, 'post77;name', 6216);
INSERT OR REPLACE INTO accounts VALUES(78, 'post78;name', 4422);
INSERT OR REPLACE INTO accounts VALUES(79, 'post79;name', 9125);
CREATE VIEW rich AS SELECT id, name FROM accounts WHERE balance > 9000;
CREATE INDEX idx_balance ON accounts(balance);
