# backend/config/__init__.py
try:
    import MySQLdb  # noqa: F401 — use real mysqlclient if available
except ImportError:
    # Fallback to PyMySQL for environments where mysqlclient won't build
    import pymysql
    pymysql.version_info = (2, 2, 1, "final", 0)
    pymysql.install_as_MySQLdb()
