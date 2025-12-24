import os
import logging
from typing import Optional, List, Dict, Any, Iterator, Union
from contextlib import contextmanager
from types import TracebackType
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker, Session
from sqlalchemy.exc import SQLAlchemyError, OperationalError
from sqlalchemy.engine import Engine

logger = logging.getLogger(__name__)

# Try to import jaydebeapi for HSQLDB support
try:
    import jaydebeapi  # type: ignore
    _HSQLDB_AVAILABLE = True
except ImportError:
    _HSQLDB_AVAILABLE = False
    logger.warning("jaydebeapi not available. HSQLDB fallback will not work.")


class PostgreSqllDbConnection:
    """
    Database connection class using SQLAlchemy.
    Supports PostgreSQL and HSQLDB (fallback).
    Supports insert, update, and select operations.
    """
    
    def __init__(self, db_name: str, host: Optional[str] = None, 
                 port: Optional[str] = None, user: Optional[str] = None,
                 password: Optional[str] = None, hsqldb_path: Optional[str] = None):
        """
        Initialize database connection.
        
        Args:
            db_name: Database name
            host: Database host (defaults to POSTGRES_HOST env var)
            port: Database port (defaults to POSTGRES_PORT env var)
            user: Database user (defaults to POSTGRES_USER env var)
            password: Database password (defaults to POSTGRES_PASSWORD env var)
            hsqldb_path: Path to HSQLDB database file (defaults to ./data/{db_name}.hsqldb)
        """
        self.db_name = db_name
        self.host = host or os.getenv('POSTGRES_HOST', 'localhost')
        self.port = port or os.getenv('POSTGRES_PORT', '5432')
        self.user = user or os.getenv('POSTGRES_USER')
        self.password = password or os.getenv('POSTGRES_PASSWORD')
        self.hsqldb_path = hsqldb_path or os.getenv('HSQLDB_PATH')
        
        self.engine: Union[Engine, Any, None] = None  # type: ignore
        self.SessionLocal: Optional[sessionmaker] = None  # type: ignore
        self._connection_string: Optional[str] = None
        self._db_type: str = 'postgresql'  # 'postgresql' or 'hsqldb'
        self._hsqldb_jar_path: Optional[str] = None
        self._hsqldb_conn: Any = None  # jaydebeapi connection
        
    def _build_connection_string(self, db_type: str = 'postgresql') -> str:
        """
        Build database connection string.
        
        Args:
            db_type: 'postgresql' or 'hsqldb'
        """
        if db_type == 'postgresql':
            if not all([self.host, self.port, self.user, self.password, self.db_name]):
                raise ValueError(
                    "Missing required PostgreSQL connection parameters. "
                    "Provide host, port, user, password, and db_name or set environment variables."
                )
            return (
                f"postgresql+psycopg://{self.user}:{self.password}@"
                f"{self.host}:{self.port}/{self.db_name}"
            )
        elif db_type == 'hsqldb':
            # Build HSQLDB file path
            if self.hsqldb_path:
                db_path = Path(self.hsqldb_path)
            else:
                # Default to ./data/{db_name}.hsqldb
                data_dir = Path('./data')
                data_dir.mkdir(exist_ok=True)
                db_path = data_dir / f"{self.db_name}.hsqldb"
            
            # HSQLDB file-based connection string
            # Format: jdbc:hsqldb:file:/path/to/db
            db_path_str = str(db_path.absolute()).replace('\\', '/')
            return f"jdbc:hsqldb:file:{db_path_str}"
        else:
            raise ValueError(f"Unsupported database type: {db_type}")
    
    def _check_postgresql_database_exists(self) -> bool:
        """Check if PostgreSQL database exists by attempting connection."""
        try:
            # Try to connect to PostgreSQL server (not specific database)
            test_conn_string = (
                f"postgresql+psycopg://{self.user}:{self.password}@"
                f"{self.host}:{self.port}/postgres"  # Connect to default 'postgres' database
            )
            test_engine = create_engine(test_conn_string, pool_pre_ping=True)
            with test_engine.connect() as conn:
                # Check if database exists
                result = conn.execute(
                    text("SELECT 1 FROM pg_database WHERE datname = :db_name"),
                    {"db_name": self.db_name}
                )
                exists = result.fetchone() is not None
            test_engine.dispose()
            return exists
        except Exception as e:
            logger.warning(f"Could not check PostgreSQL database existence: {e}")
            return False
    
    def _setup_hsqldb(self) -> None:
        """Set up HSQLDB connection using jaydebeapi with SQLAlchemy wrapper."""
        if not _HSQLDB_AVAILABLE:
            raise RuntimeError(
                "HSQLDB fallback requires jaydebeapi. "
                "Install it with: pip install jaydebeapi"
            )
        
        # Find HSQLDB jar file
        hsqldb_jar = self._find_hsqldb_jar()
        if not hsqldb_jar:
            raise FileNotFoundError(
                "HSQLDB jar file not found. "
                "Please download hsqldb.jar and place it in the project directory or set HSQLDB_JAR_PATH env var."
            )
        
        self._hsqldb_jar_path = hsqldb_jar
        jdbc_url = self._build_connection_string('hsqldb')
        
        try:
            # Create a connection creator function for SQLAlchemy
            def hsqldb_connection_creator():
                return jaydebeapi.connect(
                    "org.hsqldb.jdbcDriver",
                    jdbc_url,
                    ["SA", ""],  # Default HSQLDB user/password
                    hsqldb_jar
                )
            
            # Create SQLAlchemy engine with custom connection creator
            # Use a generic database URL that SQLAlchemy can work with
            # We'll use a workaround: create engine with a dummy URL and replace the connection
            from sqlalchemy.pool import StaticPool
            
            # Create engine with custom connection creator
            # SQLAlchemy doesn't natively support JDBC, so we use a workaround
            # We'll create a custom engine that uses jaydebeapi connections
            self.engine = create_engine(
                "sqlite:///:memory:",  # Dummy URL, we'll override connections
                poolclass=StaticPool,
                creator=hsqldb_connection_creator,
                pool_pre_ping=False,
                echo=False
            )
            
            # Actually, the above won't work directly. Let me use a different approach:
            # Create a wrapper that makes jaydebeapi work with SQLAlchemy's text() and execute()
            # For now, let's store the jaydebeapi connection and adapt methods
            self._hsqldb_conn = hsqldb_connection_creator()
            self._db_type = 'hsqldb'
            
            # Create a minimal SQLAlchemy engine-like object
            # We'll need to create a custom engine wrapper
            # For simplicity, let's create a wrapper class
            class HSQLDBEngine:
                def __init__(self, conn_creator):
                    self.conn_creator = conn_creator
                    self._conn = None
                
                def connect(self):
                    # Create a new connection for each call (connection pooling handled by jaydebeapi)
                    conn = self.conn_creator()
                    return HSQLDBConnection(conn)
                
                def dispose(self):
                    if self._conn:
                        try:
                            self._conn.close()
                        except:
                            pass
                        self._conn = None
            
            class HSQLDBConnection:
                def __init__(self, conn):
                    self.conn = conn
                    self.cursor = None
                
                def __enter__(self):
                    if self.cursor is None:
                        self.cursor = self.conn.cursor()
                    return self
                
                def __exit__(self, exc_type, exc_val, exc_tb):
                    if self.cursor:
                        self.cursor.close()
                        self.cursor = None
                    if exc_type:
                        try:
                            self.conn.rollback()
                        except:
                            pass
                    else:
                        try:
                            self.conn.commit()
                        except:
                            pass
                
                def execute(self, query_obj, params=None):
                    # Convert SQLAlchemy text() to string
                    if hasattr(query_obj, 'text'):
                        query = query_obj.text
                    elif hasattr(query_obj, '__str__'):
                        query = str(query_obj)
                    else:
                        query = query_obj
                    
                    # Create cursor if needed
                    if self.cursor is None:
                        self.cursor = self.conn.cursor()
                    
                    # HSQLDB uses ? for parameters, SQLAlchemy uses :name
                    # Convert :name to ? and adjust params
                    if params and len(params) > 0:
                    # More robust conversion: replace :param with ? in order
                    param_list = []
                    param_keys = list(params.keys())
                    
                    # Simple approach: replace each :key with ? in order
                    modified_query = query
                    for key in param_keys:
                        modified_query = modified_query.replace(f":{key}", "?", 1)
                        param_list.append(params[key])
                        
                        self.cursor.execute(modified_query, param_list)
                    else:
                        self.cursor.execute(query)
                    
                    return HSQLDBResult(self.cursor)
                
                def commit(self):
                    if self.conn:
                        self.conn.commit()
                
                def rollback(self):
                    if self.conn:
                        self.conn.rollback()
            
            class HSQLDBResult:
                def __init__(self, cursor):
                    self.cursor = cursor
                    self._rows = None
                    self._columns = None
                
                def keys(self):
                    """Return column names as a list."""
                    if self._columns is None:
                        if self.cursor.description:
                            self._columns = [desc[0] for desc in self.cursor.description]
                        else:
                            self._columns = []
                    return self._columns
                
                def fetchall(self):
                    if self._rows is None:
                        try:
                            self._rows = self.cursor.fetchall()  # type: ignore
                        except Exception:
                            self._rows = []
                    return self._rows
                
                def fetchone(self):
                    try:
                        return self.cursor.fetchone()  # type: ignore
                    except Exception:
                        return None
                
                @property
                def rowcount(self):
                    try:
                        return self.cursor.rowcount  # type: ignore
                    except Exception:
                        return 0
            
            # Create engine wrapper
            self.engine = HSQLDBEngine(hsqldb_connection_creator)  # type: ignore
            
            # Create session factory (simplified for HSQLDB)
            class HSQLDBSession:  # type: ignore
                def __init__(self, engine):
                    self.engine = engine
                    self._conn = None
                
                def __enter__(self):
                    self._conn = self.engine.connect()
                    return self._conn
                
                def __exit__(self, exc_type, exc_val, exc_tb):
                    if self._conn:
                        self._conn.__exit__(exc_type, exc_val, exc_tb)
                
                def commit(self):
                    if self._conn:
                        self._conn.commit()
                
                def rollback(self):
                    if self._conn:
                        self._conn.rollback()
                
                def close(self):
                    if self._conn:
                        self._conn.__exit__(None, None, None)
            
            def hsqldb_session_factory():  # type: ignore
                return HSQLDBSession(self.engine)
            
            self.SessionLocal = hsqldb_session_factory  # type: ignore
            
            logger.info(f"Connected to HSQLDB database: {jdbc_url}")
            
        except Exception as e:
            logger.error(f"Failed to connect to HSQLDB: {e}")
            raise
    
    def _find_hsqldb_jar(self) -> Optional[str]:
        """Find HSQLDB jar file in common locations."""
        # Check environment variable first
        jar_path = os.getenv('HSQLDB_JAR_PATH')
        if jar_path and Path(jar_path).exists():
            return jar_path
        
        # Check common locations
        possible_paths = [
            Path('./hsqldb.jar'),
            Path('./lib/hsqldb.jar'),
            Path('./data/hsqldb.jar'),
            Path.home() / '.local' / 'lib' / 'hsqldb.jar',
        ]
        
        for path in possible_paths:
            if path.exists():
                return str(path.absolute())
        
        return None
    
    def connect(self) -> None:
        """
        Create SQLAlchemy engine and session factory.
        Tries PostgreSQL first, falls back to HSQLDB if database doesn't exist.
        """
        # Try PostgreSQL first
        try:
            self._connection_string = self._build_connection_string('postgresql')
            test_engine = create_engine(
                self._connection_string,
                pool_pre_ping=True,
                pool_recycle=3600,
                echo=False
            )
            # Test the connection
            with test_engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            # Connection successful, use PostgreSQL
            self.engine = test_engine
            self.SessionLocal = sessionmaker(
                bind=self.engine,
                autocommit=False,
                autoflush=False
            )
            self._db_type = 'postgresql'
            logger.info("Successfully connected to PostgreSQL database: %s", self.db_name)
            return
        except (OperationalError, SQLAlchemyError, ValueError) as e:
            logger.warning(
                "Failed to connect to PostgreSQL database: %s. "
                "Attempting to fall back to HSQLDB...", str(e)
            )
            # PostgreSQL failed, try HSQLDB fallback
            try:
                self._setup_hsqldb()
                logger.info("Successfully connected to HSQLDB database: %s", self.db_name)
            except Exception as hsqldb_error:
                logger.error(
                    "Failed to connect to both PostgreSQL and HSQLDB. "
                    "PostgreSQL error: %s. HSQLDB error: %s",
                    str(e), str(hsqldb_error)
                )
                raise RuntimeError(
                    f"Failed to connect to any database. "
                    f"PostgreSQL error: {e}. HSQLDB error: {hsqldb_error}"
                ) from hsqldb_error
    
    @contextmanager
    def get_session(self) -> Iterator[Session]:
        """
        Context manager for database sessions.
        Automatically commits on success and rolls back on error.
        
        Usage:
            with db.get_session() as session:
                # perform database operations
                pass
        """
        if not self.SessionLocal:
            self.connect()
        
        if self.SessionLocal is None:
            raise RuntimeError("Session factory not initialized")
        
        session = self.SessionLocal()  # type: ignore
        try:
            yield session
            session.commit()  # type: ignore
        except Exception as e:
            session.rollback()  # type: ignore
            logger.error("Database session error: %s", str(e))
            raise
        finally:
            session.close()  # type: ignore
    
    def select(self, query: str, params: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        """
        Execute a SELECT query and return results as a list of dictionaries.
        
        Args:
            query: SQL SELECT query string
            params: Optional dictionary of parameters for parameterized queries
            
        Returns:
            List of dictionaries, where each dictionary represents a row
            
        Example:
            results = db.select("SELECT * FROM stocks WHERE symbol = :symbol", 
                              {"symbol": "AAPL"})
        """
        if not self.engine:
            self.connect()
        
        assert self.engine is not None, "Engine not initialized"
        
        try:
            with self.engine.connect() as conn:
                result = conn.execute(text(query), params or {})
                columns = result.keys()
                rows = result.fetchall()
                return [dict(zip(columns, row)) for row in rows]
        except SQLAlchemyError as e:
            logger.error("Select query error: %s", str(e))
            raise
    
    def select_one(self, query: str, params: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
        """
        Execute a SELECT query and return the first result as a dictionary.
        
        Args:
            query: SQL SELECT query string
            params: Optional dictionary of parameters for parameterized queries
            
        Returns:
            Dictionary representing the first row, or None if no results
        """
        results = self.select(query, params)
        return results[0] if results else None
    
    def insert(self, table_name: str, data: Dict[str, Any]) -> int:
        """
        Insert a single row into a table.
        
        Args:
            table_name: Name of the table
            data: Dictionary of column names and values
            
        Returns:
            The ID of the inserted row (if table has an auto-incrementing primary key)
            
        Example:
            row_id = db.insert("stocks", {"symbol": "AAPL", "price": 150.25})
        """
        if not self.engine:
            self.connect()
        
        assert self.engine is not None, "Engine not initialized"
        
        try:
            with self.engine.connect() as conn:
                # Try to get the primary key column name (assuming it's 'id')
                query = f"INSERT INTO {table_name} ({', '.join(data.keys())}) VALUES ({', '.join([f':{k}' for k in data.keys()])})"
                
                # Try to return the ID if possible
                if self._db_type == 'postgresql':
                    try:
                        query_with_return = query + " RETURNING id"
                        result = conn.execute(text(query_with_return), data)
                        conn.commit()
                        row = result.fetchone()
                        return int(row[0]) if row and row[0] is not None else 0
                    except SQLAlchemyError:
                        # If RETURNING doesn't work, just execute the insert
                        result = conn.execute(text(query), data)
                        conn.commit()
                        return result.rowcount
                else:  # HSQLDB
                    # HSQLDB uses IDENTITY() to get the last inserted ID
                    try:
                        result = conn.execute(text(query), data)
                        conn.commit()
                        # Get the last inserted ID
                        id_result = conn.execute(text("CALL IDENTITY()"), {})
                        id_row = id_result.fetchone()
                        return int(id_row[0]) if id_row and id_row[0] is not None else result.rowcount
                    except SQLAlchemyError:
                        # If IDENTITY() doesn't work, just execute the insert
                        result = conn.execute(text(query), data)
                        conn.commit()
                        return result.rowcount
        except SQLAlchemyError as e:
            logger.error("Insert error: %s", str(e))
            raise
    
    def insert_many(self, table_name: str, data_list: List[Dict[str, Any]]) -> int:
        """
        Insert multiple rows into a table in a single transaction.
        
        Args:
            table_name: Name of the table
            data_list: List of dictionaries, each representing a row
            
        Returns:
            Number of rows inserted
            
        Example:
            count = db.insert_many("stocks", [
                {"symbol": "AAPL", "price": 150.25},
                {"symbol": "GOOGL", "price": 2500.50}
            ])
        """
        if not data_list:
            return 0
        
        if not self.engine:
            self.connect()
        
        assert self.engine is not None, "Engine not initialized"
        
        try:
            columns = list(data_list[0].keys())
            placeholders = ', '.join([f':{col}' for col in columns])
            query = f"INSERT INTO {table_name} ({', '.join(columns)}) VALUES ({placeholders})"
            
            with self.engine.connect() as conn:
                result = conn.execute(text(query), data_list)
                conn.commit()
                return result.rowcount
        except SQLAlchemyError as e:
            logger.error("Bulk insert error: %s", str(e))
            raise
    
    def update(self, table_name: str, data: Dict[str, Any], 
               where_clause: str, where_params: Optional[Dict[str, Any]] = None) -> int:
        """
        Update rows in a table.
        
        Args:
            table_name: Name of the table
            data: Dictionary of column names and new values
            where_clause: WHERE clause (e.g., "id = :id" or "symbol = :symbol")
            where_params: Optional dictionary of parameters for WHERE clause
            
        Returns:
            Number of rows updated
            
        Example:
            count = db.update("stocks", 
                            {"price": 155.00}, 
                            "symbol = :symbol",
                            {"symbol": "AAPL"})
        """
        if not self.engine:
            self.connect()
        
        assert self.engine is not None, "Engine not initialized"
        
        try:
            set_clause = ', '.join([f"{k} = :{k}" for k in data.keys()])
            query = f"UPDATE {table_name} SET {set_clause} WHERE {where_clause}"
            
            # Merge data and where_params
            params = {**data, **(where_params or {})}
            
            with self.engine.connect() as conn:
                result = conn.execute(text(query), params)
                conn.commit()
                return result.rowcount
        except SQLAlchemyError as e:
            logger.error("Update error: %s", str(e))
            raise
    
    def delete(self, table_name: str, where_clause: str, 
               where_params: Optional[Dict[str, Any]] = None) -> int:
        """
        Delete rows from a table.
        
        Args:
            table_name: Name of the table
            where_clause: WHERE clause (e.g., "id = :id")
            where_params: Optional dictionary of parameters for WHERE clause
            
        Returns:
            Number of rows deleted
            
        Example:
            count = db.delete("stocks", "symbol = :symbol", {"symbol": "AAPL"})
        """
        if not self.engine:
            self.connect()
        
        assert self.engine is not None, "Engine not initialized"
        
        try:
            query = f"DELETE FROM {table_name} WHERE {where_clause}"
            
            with self.engine.connect() as conn:
                result = conn.execute(text(query), where_params or {})
                conn.commit()
                return result.rowcount
        except SQLAlchemyError as e:
            logger.error("Delete error: %s", str(e))
            raise
    
    def execute_raw(self, query: str, params: Optional[Dict[str, Any]] = None) -> Any:
        """
        Execute a raw SQL query (for complex operations).
        
        Args:
            query: Raw SQL query string
            params: Optional dictionary of parameters
            
        Returns:
            Query result (depends on query type)
        """
        if not self.engine:
            self.connect()
        
        assert self.engine is not None, "Engine not initialized"
        
        try:
            with self.engine.connect() as conn:
                result = conn.execute(text(query), params or {})
                conn.commit()
                return result
        except SQLAlchemyError as e:
            logger.error("Raw query execution error: %s", str(e))
            raise
    
    def close(self) -> None:
        """Close the database connection and dispose of the engine."""
        if self.engine:
            self.engine.dispose()
            logger.info("Database connection closed")
    
    def __enter__(self) -> 'PostgreSqllDbConnection':
        """Context manager entry."""
        if not self.engine:
            self.connect()
        return self
    
    def __exit__(self, exc_type: Optional[type[BaseException]], 
                 exc_val: Optional[BaseException], 
                 exc_tb: Optional[TracebackType]) -> None:
        """Context manager exit."""
        self.close()