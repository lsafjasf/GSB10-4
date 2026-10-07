-- 演示脚本：字符串/注释/续行中的分号都不应切断语句
INSERT INTO t VALUES ('a;b', "c;d", `e;f`);
# 井号注释 ;
/* 块注释 ; 内含分号 */ INSERT INTO t VALUES ('it''s ok');
UPDATE t SET x = 1 \
+ 2 WHERE id = 3;
DELETE FROM t WHERE note = '--not a comment;';
