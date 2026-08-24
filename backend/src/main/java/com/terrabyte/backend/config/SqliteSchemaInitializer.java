package com.terrabyte.backend.config;

import java.sql.Connection;
import java.sql.ResultSet;
import java.sql.Statement;
import java.util.List;

import javax.sql.DataSource;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Qualifier;
import org.springframework.boot.context.event.ApplicationReadyEvent;
import org.springframework.context.annotation.Configuration;
import org.springframework.context.event.EventListener;
import org.springframework.core.io.ClassPathResource;
import org.springframework.core.io.Resource;

/**
 * 애플리케이션 시작 시 SQLite 점수 DB의 스키마와 마이그레이션을
 * 자동으로 적용합니다.
 *
 * <ul>
 *   <li>{@code axis_catalog} 테이블이 없으면 → {@code schema.sql} 전체 실행 (신규 DB)</li>
 *   <li>이미 존재하면 → 마이그레이션 스크립트만 멱등 실행</li>
 * </ul>
 *
 * SQL 파일은 Gradle {@code prepareSqliteResources} 태스크가 빌드 시
 * classpath {@code db/sqlite/} 아래에 복사합니다.
 */
@Configuration(proxyBeanMethods = false)
class SqliteSchemaInitializer {

    private static final Logger log = LoggerFactory.getLogger(SqliteSchemaInitializer.class);

    private static final String SCHEMA_RESOURCE = "db/sqlite/schema.sql";

    /**
     * 마이그레이션 스크립트 목록. 파일 이름순으로 정렬되어야 합니다.
     * schema.sql에 이미 반영된 내용이더라도 IF NOT EXISTS / INSERT OR IGNORE로
     * 작성되어 있어 반복 실행해도 안전합니다.
     */
    private static final List<String> MIGRATION_RESOURCES = List.of(
            "db/sqlite/migrations/2026-07-25_score_profiles_v2.sql",
            "db/sqlite/migrations/2026-07-25_score_model_config_v1.sql",
            "db/sqlite/migrations/2026-08-24_score_profiles_v3_ppfd_compensation.sql"
    );

    private final DataSource scoreDataSource;

    SqliteSchemaInitializer(@Qualifier("scoreDataSource") DataSource scoreDataSource) {
        this.scoreDataSource = scoreDataSource;
    }

    @EventListener(ApplicationReadyEvent.class)
    void initializeSchema() {
        if (isSchemaPresent()) {
            log.info("SQLite score DB schema already present — running migrations only");
            runMigrations();
        } else {
            log.info("SQLite score DB is empty — applying full schema");
            executeSqlFromClasspath(SCHEMA_RESOURCE);
            log.info("SQLite score DB schema applied successfully");
        }
    }

    private boolean isSchemaPresent() {
        try (Connection conn = scoreDataSource.getConnection();
             Statement stmt = conn.createStatement();
             ResultSet rs = stmt.executeQuery(
                     "SELECT count(*) FROM sqlite_master WHERE type='table' AND name='axis_catalog'")) {
            return rs.next() && rs.getInt(1) > 0;
        } catch (Exception e) {
            log.warn("Failed to check SQLite schema state, will attempt full init", e);
            return false;
        }
    }

    private void runMigrations() {
        for (String path : MIGRATION_RESOURCES) {
            try {
                executeSqlFromClasspath(path);
                log.debug("Applied migration: {}", path);
            } catch (Exception e) {
                log.warn("Migration {} failed (may already be applied): {}", path, e.getMessage());
            }
        }
    }

    /**
     * classpath의 SQL 파일을 읽어 한 번에 실행합니다.
     * <p>
     * SQLite JDBC는 {@link Statement#execute(String)}로 여러 문장을 한 번에
     * 실행할 수 있으므로 세미콜론 분리 없이 전체 SQL을 넘깁니다.
     * 이 방식은 {@code CREATE TRIGGER} 본문 안의 세미콜론이나
     * {@code BEGIN/COMMIT} 트랜잭션 제어문도 올바르게 처리합니다.
     */
    private void executeSqlFromClasspath(String classpathLocation) {
        Resource resource = new ClassPathResource(classpathLocation);
        if (!resource.exists()) {
            log.warn("SQL resource not found on classpath: {}", classpathLocation);
            return;
        }
        try (Connection conn = scoreDataSource.getConnection();
             Statement stmt = conn.createStatement()) {
            String sql = new String(
                    resource.getInputStream().readAllBytes(),
                    java.nio.charset.StandardCharsets.UTF_8);
            stmt.execute(sql);
        } catch (Exception e) {
            throw new RuntimeException(
                    "Failed to execute SQL from classpath: " + classpathLocation, e);
        }
    }
}
