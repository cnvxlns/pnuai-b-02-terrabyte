package com.terrabyte.backend.rule;

import static org.assertj.core.api.Assertions.assertThat;

import java.util.List;

import com.terrabyte.backend.measurement.MeasurementStore;
import com.terrabyte.backend.pot.Pot;
import com.terrabyte.backend.pot.PotRepository;

import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.beans.factory.annotation.Qualifier;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.test.context.ActiveProfiles;
import org.springframework.test.context.bean.override.mockito.MockitoBean;

/**
 * Which pots the rule engine is even offered.
 *
 * <p>The filter lives in SQL rather than in the engine, so this is where it can
 * be tested. A deployment with a thousand pots must not load them all into
 * memory to discard most of them, and the engine trusting the query is what
 * keeps the condition in one place.
 */
@SpringBootTest
@ActiveProfiles("test")
class AutomaticControlSelectionTests {

    private static final long POT_ID = 1L;

    @Autowired private PotRepository pots;

    @Autowired
    @Qualifier("postgresJdbcTemplate")
    private JdbcTemplate jdbcTemplate;

    @MockitoBean private MeasurementStore measurementStore;

    @BeforeEach
    void setUp() {
        jdbcTemplate.update(
                "UPDATE pot SET status = 'OFFLINE', auto_control_enabled = TRUE");
        jdbcTemplate.update(
                "UPDATE pot SET status = 'ONLINE', crop_code = 'lettuce',"
                        + " crop_selected_at = CURRENT_TIMESTAMP WHERE id = ?",
                POT_ID);
    }

    @AfterEach
    void tearDown() {
        jdbcTemplate.update("UPDATE pot SET status = 'OFFLINE', auto_control_enabled = TRUE");
    }

    @Test
    void anOnlinePotWithACropIsOffered() {
        assertThat(ids()).contains(POT_ID);
    }

    @Test
    void thePotsOwnerCanTakeItOutOfAutomaticControl() {
        assertThat(pots.setAutoControl(POT_ID, false)).isTrue();

        assertThat(ids()).doesNotContain(POT_ID);
    }

    @Test
    void switchingItBackOnPutsItBack() {
        pots.setAutoControl(POT_ID, false);

        pots.setAutoControl(POT_ID, true);

        assertThat(ids()).contains(POT_ID);
    }

    @Test
    void aPotWithNoCropIsNotOffered() {
        jdbcTemplate.update(
                "UPDATE pot SET crop_code = NULL, crop_selected_at = NULL WHERE id = ?", POT_ID);

        // No crop means no thresholds to compare a reading against.
        assertThat(ids()).doesNotContain(POT_ID);
    }

    @Test
    void anOfflinePotIsNotOffered() {
        jdbcTemplate.update("UPDATE pot SET status = 'OFFLINE' WHERE id = ?", POT_ID);

        // A command for it would expire before anything could run it, and the
        // refusal would still be charged to the pot's decision history.
        assertThat(ids()).doesNotContain(POT_ID);
    }

    @Test
    void existingPotsDefaultToAutomaticControl() {
        // The migration backfills TRUE. Backfilling FALSE would stop irrigation
        // on every pot in the field with one deploy and no error anywhere.
        assertThat(pots.findById(POT_ID).map(Pot::autoControlEnabled)).contains(true);
    }

    private List<Long> ids() {
        return pots.findAllUnderAutomaticControl().stream().map(Pot::id).toList();
    }
}
