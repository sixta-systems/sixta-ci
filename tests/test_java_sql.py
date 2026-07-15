"""Phase S3: SQL embedded in Java (@Query nativeQuery, Spring Data JDBC,
JdbcTemplate/JdbcClient/createNativeQuery) and MyBatis XML mappers
(docs/spring-boot-support.md). All offline."""

import pytest

import sixta_review as sr


def _opts(**overrides):
    opts = sr.build_parser().parse_args(["--api", "v1", "--engine", "postgresql"])
    opts.schema_cmd = None
    for k, v in overrides.items():
        setattr(opts, k, v)
    return opts


# --------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------

def test_java_sql_target_requires_hint(tmp_path):
    with_sql = tmp_path / "OrderRepository.java"
    with_sql.write_text('class X { @Query(nativeQuery = true, value = "SELECT 1") void f(); }')
    plain = tmp_path / "OrderService.java"
    plain.write_text("class OrderService { int x; }")
    assert sr.java_sql_target(str(with_sql))
    assert not sr.java_sql_target(str(plain))
    assert sr.is_migration_file(str(with_sql))


def test_flyway_java_migration_wins_over_java_sql(tmp_path):
    d = tmp_path / "db" / "migration"
    d.mkdir(parents=True)
    f = d / "V3__Backfill.java"
    f.write_text('class V3__Backfill { void migrate(Context c) { c.getConnection().createStatement().execute("UPDATE t SET x = 0"); } }')
    # flagged as a Flyway Java migration (manual review), not string-scraped
    sql, manual = sr.extract_migration(str(f), _opts())
    assert sql == "" and manual[0].check_name == "flyway-java-manual-review"


# --------------------------------------------------------------------------
# Annotations
# --------------------------------------------------------------------------

def test_native_query_annotation_extracted():
    src = '''
    public interface OrderRepo extends JpaRepository<Order, Long> {
        @Query(value = "SELECT * FROM orders WHERE status = :status", nativeQuery = true)
        List<Order> byStatus(@Param("status") String status);
    }
    '''
    assert sr.extract_java_sql(src) == ["SELECT * FROM orders WHERE status = :status"]


def test_jpql_query_is_skipped():
    src = '''
    import org.springframework.data.jpa.repository.Query;
    public interface OrderRepo {
        @Query("SELECT o FROM Order o WHERE o.status = ?1")
        List<Order> byStatus(String status);
    }
    '''
    assert sr.extract_java_sql(src) == []


def test_spring_data_jdbc_query_is_always_native():
    src = '''
    import org.springframework.data.jdbc.repository.query.Query;
    public interface OrderRepo {
        @Query("SELECT * FROM orders WHERE status = :status")
        List<Order> byStatus(String status);
    }
    '''
    assert sr.extract_java_sql(src) == ["SELECT * FROM orders WHERE status = :status"]


def test_named_native_query_takes_query_attr_not_name():
    src = '@NamedNativeQuery(name = "Order.byStatus", query = "SELECT * FROM orders", resultClass = Order.class)'
    assert sr.extract_java_sql(src) == ["SELECT * FROM orders"]


def test_text_block_and_concatenation():
    src = '''
    @Query(nativeQuery = true, value = """
        SELECT o.id, o.total
        FROM orders o
        WHERE o.status = :status
        """)
    List<Order> a();
    void b(JdbcTemplate t) { t.update("UPDATE orders SET status = ?" + " WHERE id = ?"); }
    '''
    out = sr.extract_java_sql(src)
    assert any("FROM orders o" in f for f in out)
    assert "UPDATE orders SET status = ? WHERE id = ?" in out


def test_constant_resolution_in_call_site():
    src = '''
    class Dao {
        private static final String FIND = "SELECT * FROM orders " + "WHERE customer_id = ?";
        List<Order> find(JdbcTemplate t) { return t.query(FIND, mapper); }
    }
    '''
    assert sr.extract_java_sql(src) == ["SELECT * FROM orders WHERE customer_id = ?"]


def test_jdbcclient_and_createnativequery():
    src = '''
    void f(JdbcClient c, EntityManager em) {
        c.sql("SELECT count(*) FROM orders WHERE status = :status").param("status", s);
        em.createNativeQuery("DELETE FROM orders WHERE id = ?1").executeUpdate();
    }
    '''
    out = sr.extract_java_sql(src)
    assert "SELECT count(*) FROM orders WHERE status = :status" in out
    assert "DELETE FROM orders WHERE id = ?" in out  # ?1 normalized


def test_non_sql_strings_ignored_and_deduped():
    src = '''
    void f(JdbcTemplate t) {
        log.info("update failed for order");
        t.execute("refresh-cache");
        t.update("DELETE FROM audit WHERE ts < ?");
        t.update("DELETE FROM audit WHERE ts < ?");
    }
    '''
    out = sr.extract_java_sql(src)
    assert out == ["DELETE FROM audit WHERE ts < ?"]


def test_spel_normalization():
    src = '@Query(nativeQuery = true, value = "SELECT * FROM t WHERE tenant = :#{principal.tenant} AND id = ?1")'
    assert sr.extract_java_sql(src) == ["SELECT * FROM t WHERE tenant = :spel_param AND id = ?"]


def test_extract_migration_java_joins_fragments(tmp_path):
    f = tmp_path / "Dao.java"
    f.write_text('void f(JdbcTemplate t) { t.update("DELETE FROM a WHERE id = ?"); t.update("DELETE FROM b WHERE id = ?"); }')
    sql, manual = sr.extract_migration(str(f), _opts())
    assert manual is None
    assert sr.split_statements(sql) == ["DELETE FROM a WHERE id = ?", "DELETE FROM b WHERE id = ?"]


def test_extract_migration_java_without_sql_skips(tmp_path):
    f = tmp_path / "Service.java"
    f.write_text('class S { JdbcTemplate t; void f() { log.info("no sql here"); } }')
    assert sr.extract_migration(str(f), _opts()) is None


# --------------------------------------------------------------------------
# MyBatis mappers
# --------------------------------------------------------------------------

MAPPER = '''<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE mapper PUBLIC "-//mybatis.org//DTD Mapper 3.0//EN" "http://mybatis.org/dtd/mybatis-3-mapper.dtd">
<mapper namespace="com.example.OrderMapper">
  <sql id="cols">id, customer_id, status</sql>
  <select id="byStatus" resultType="Order">
    SELECT <include refid="cols"/> FROM orders
    <where>
      <if test="status != null">AND status = #{status}</if>
    </where>
    ORDER BY ${sortColumn}
  </select>
  <update id="touch">
    UPDATE orders
    <set>updated_at = now(),</set>
    WHERE id = #{id}
  </update>
</mapper>
'''


def test_mybatis_target_and_discovery(tmp_path):
    m = tmp_path / "OrderMapper.xml"
    m.write_text(MAPPER)
    other = tmp_path / "logback.xml"
    other.write_text("<configuration/>")
    assert sr.mybatis_mapper_target(str(m))
    assert not sr.mybatis_mapper_target(str(other))
    assert sr.is_migration_file(str(m))


def test_mybatis_extraction_flattens_dynamic_sql():
    fragments, uses_dollar = sr.extract_mybatis_sql(MAPPER)
    assert uses_dollar
    select = next(f for f in fragments if f.startswith("SELECT"))
    assert "id, customer_id, status" in select        # <include> resolved
    assert "WHERE status = ?" in select               # <where> + <if> flattened, #{} -> ?
    assert "ORDER BY sortColumn" in select            # ${} -> bare token
    update = next(f for f in fragments if f.startswith("UPDATE"))
    assert "SET updated_at = now()" in update         # <set> flattened, trailing comma dropped


def test_mybatis_choose_takes_first_when():
    xml = '''<mapper namespace="m"><select id="s">
      SELECT * FROM t
      <choose>
        <when test="a">WHERE a = #{a}</when>
        <when test="b">WHERE b = #{b}</when>
        <otherwise>WHERE 1 = 1</otherwise>
      </choose>
    </select></mapper>'''
    fragments, _ = sr.extract_mybatis_sql(xml)
    assert fragments == ["SELECT * FROM t WHERE a = ?"]


def test_mybatis_dollar_interpolation_flags_info_finding(tmp_path):
    """Advisory (Info): a local severity can't gate consistently across api
    modes, since v1's server worst_severity is authoritative."""
    m = tmp_path / "OrderMapper.xml"
    m.write_text(MAPPER)
    sql, manual = sr.extract_migration(str(m), _opts())
    assert "SELECT" in sql
    finding, section = manual
    assert finding.check_name == "mybatis-string-interpolation"
    assert finding.severity == "Info"
    assert "${" in section or "interpolation" in section


def test_mybatis_unparseable_xml_raises_for_skip(tmp_path):
    m = tmp_path / "Broken.xml"
    m.write_text('<mapper namespace="x"><select id="s">SELECT 1</wrong>')
    with pytest.raises(RuntimeError, match="did not parse"):
        sr.extract_migration(str(m), _opts())


# --------------------------------------------------------------------------
# v1 integration: java DML rides as query extractions
# --------------------------------------------------------------------------

class _Capture:
    def __init__(self):
        self.request = None

    def analyze_v1(self, request):
        self.request = request
        return {"results": [], "worst_severity": None}


def test_run_v1_java_dml_becomes_query_extractions(tmp_path):
    f = tmp_path / "Dao.java"
    f.write_text('void f(JdbcTemplate t) { t.query("SELECT * FROM orders WHERE status = ?", m); }')
    client = _Capture()
    sr.run_v1([str(f)], _opts(), client, hints={})
    kinds = [e["kind"] for e in client.request["extractions"]]
    assert kinds == ["query"]
    assert client.request["extractions"][0]["sql"] == "SELECT * FROM orders WHERE status = ?"


# --------------------------------------------------------------------------
# Review-pass regression tests (adversarial findings, each reproduced first)
# --------------------------------------------------------------------------

def test_jpql_only_file_is_not_discovered(tmp_path):
    """Bare @Query (JPQL) must not trigger discovery: it extracts nothing and
    would leave 'analyzed, 0 findings' noise on every JPA PR."""
    f = tmp_path / "Repo.java"
    f.write_text('import org.springframework.data.jpa.repository.Query;\n'
                 'interface R { @Query("SELECT o FROM Order o") List<Order> a(); }')
    assert not sr.java_sql_target(str(f))


def test_data_jdbc_file_is_discovered_via_import(tmp_path):
    f = tmp_path / "Repo.java"
    f.write_text('import org.springframework.data.jdbc.repository.query.Query;\n'
                 'interface R { @Query("SELECT * FROM orders") List<Order> a(); }')
    assert sr.java_sql_target(str(f))


def test_constant_with_semicolon_inside_string():
    src_java = (
        'class D {\n'
        '  static final String CLEANUP = "DELETE FROM audit_log WHERE created < now() - interval \'90 days\';";\n'
        '  void f(JdbcTemplate t) { t.update(CLEANUP); }\n'
        '}'
    )
    assert sr.extract_java_sql(src_java) == ["DELETE FROM audit_log WHERE created < now() - interval '90 days';"]


def test_comment_with_unbalanced_paren_inside_annotation():
    src_java = ('@Query(value = "SELECT * FROM orders WHERE id = ?1", // legacy lookup :)\n'
                '       nativeQuery = true)\n'
                'List<Order> byId(long id);')
    assert sr.extract_java_sql(src_java) == ["SELECT * FROM orders WHERE id = ?"]


def test_dynamic_concatenation_is_skipped_not_holed():
    """'SELECT * FROM ' + TABLE with an unresolvable operand must be skipped:
    analyzing 'SELECT * FROM  WHERE ...' would mislead."""
    src_java = ('void f(JdbcTemplate t, String table) {\n'
                '  t.query("SELECT * FROM " + table + " WHERE id = ?", m);\n'
                '}')
    assert sr.extract_java_sql(src_java) == []


def test_concatenation_through_constant_resolves():
    src_java = ('class D { static final String T = "orders";\n'
                '  void f(JdbcTemplate t) { t.query("SELECT * FROM " + T + " WHERE id = ?", m); } }')
    assert sr.extract_java_sql(src_java) == ["SELECT * FROM orders WHERE id = ?"]


def test_mybatis_selectkey_not_merged_into_parent():
    xml = ('<mapper namespace="m"><insert id="i">\n'
           '  <selectKey keyProperty="id" resultType="long" order="AFTER">SELECT LAST_INSERT_ID()</selectKey>\n'
           '  INSERT INTO orders (total) VALUES (#{total})\n'
           '</insert></mapper>')
    fragments, _ = sr.extract_mybatis_sql(xml)
    assert fragments == ["INSERT INTO orders (total) VALUES (?)"]


def test_mybatis_trim_prefix_and_overrides():
    xml = ('<mapper namespace="m"><select id="s">SELECT * FROM orders\n'
           '  <trim prefix="WHERE" prefixOverrides="AND |OR ">AND status = #{s}</trim>\n'
           '</select></mapper>')
    fragments, _ = sr.extract_mybatis_sql(xml)
    assert fragments == ["SELECT * FROM orders WHERE status = ?"]


def test_mybatis_foreach_open_close():
    xml = ('<mapper namespace="m"><select id="s">SELECT * FROM orders WHERE id IN\n'
           '  <foreach item="i" collection="ids" open="(" separator="," close=")">#{i}</foreach>\n'
           '</select></mapper>')
    fragments, _ = sr.extract_mybatis_sql(xml)
    assert fragments == ["SELECT * FROM orders WHERE id IN (?)"]


def test_mybatis_unresolvable_include_skips_statement():
    xml = ('<mapper namespace="m"><select id="s">SELECT <include refid="com.other.Mapper.cols"/> FROM orders</select>'
           '<select id="t">SELECT id FROM orders</select></mapper>')
    fragments, _ = sr.extract_mybatis_sql(xml)
    assert fragments == ["SELECT id FROM orders"]  # incomplete one skipped, sibling kept


def test_mybatis_cyclic_include_does_not_crash():
    xml = ('<mapper namespace="m"><sql id="a"><include refid="a"/></sql>'
           '<select id="s">SELECT <include refid="a"/> FROM orders</select></mapper>')
    fragments, _ = sr.extract_mybatis_sql(xml)
    assert fragments == []
