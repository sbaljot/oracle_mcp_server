SET SERVEROUTPUT ON;

DECLARE
    v_sql VARCHAR2(4000);
BEGIN
    FOR object_row IN (
        SELECT object_type, object_name
        FROM user_objects
        WHERE generated = 'N'
          AND object_name <> 'DBTOOLS$MCP_LOG'
          AND object_type IN (
              'VIEW', 'MATERIALIZED VIEW', 'TRIGGER',
              'PROCEDURE', 'FUNCTION', 'PACKAGE',
              'SYNONYM', 'SEQUENCE', 'TYPE', 'TABLE'
          )
        ORDER BY CASE object_type
            WHEN 'VIEW' THEN 1
            WHEN 'MATERIALIZED VIEW' THEN 2
            WHEN 'TRIGGER' THEN 3
            WHEN 'PROCEDURE' THEN 4
            WHEN 'FUNCTION' THEN 5
            WHEN 'PACKAGE' THEN 6
            WHEN 'SYNONYM' THEN 7
            WHEN 'SEQUENCE' THEN 8
            WHEN 'TYPE' THEN 9
            WHEN 'TABLE' THEN 10
        END
    ) LOOP
        v_sql := CASE object_row.object_type
            WHEN 'TABLE' THEN
                'DROP TABLE "' || object_row.object_name || '" CASCADE CONSTRAINTS PURGE'
            WHEN 'TYPE' THEN
                'DROP TYPE "' || object_row.object_name || '" FORCE'
            WHEN 'MATERIALIZED VIEW' THEN
                'DROP MATERIALIZED VIEW "' || object_row.object_name || '"'
            ELSE
                'DROP ' || object_row.object_type || ' "' || object_row.object_name || '"'
        END;

        BEGIN
            EXECUTE IMMEDIATE v_sql;
            DBMS_OUTPUT.PUT_LINE('Dropped: ' || v_sql);
        EXCEPTION
            WHEN OTHERS THEN
                DBMS_OUTPUT.PUT_LINE('Failed: ' || v_sql || ' - ' || SQLERRM);
        END;
    END LOOP;
END;
/