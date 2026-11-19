package db.callback;

import java.io.OutputStream;
import java.nio.charset.StandardCharsets;
import java.time.Instant;
import java.time.temporal.ChronoUnit;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;
import java.util.concurrent.TimeUnit;

import org.flywaydb.core.api.MigrationInfo;
import org.flywaydb.core.api.callback.Callback;
import org.flywaydb.core.api.callback.Context;
import org.flywaydb.core.api.callback.Event;

/**
 * Tells SIXTA when Flyway applied migrations (a sixta-ci recipe).
 *
 * <p>Where it goes: copy this file to {@code src/main/java/db/callback/} in the service that runs
 * Flyway. Flyway looks for callbacks in the {@code db/callback} package, so nothing else is
 * configured; if your Flyway is configured with its own {@code callbacks} list, add
 * {@code db.callback.SixtaRegisterCallback} to it (in Spring Boot, declaring it as a bean does the
 * same). For the Flyway command line, compile it into a jar in Flyway's {@code jars} directory, or
 * use {@code flyway-migrate.sh} beside this file, which needs no Java.
 *
 * <p>What it does: afterEachMigrate notes each migration Flyway applied (its script, version,
 * start, finish, and the statements Flyway executed, placeholders already replaced), and
 * afterMigrate posts them, one registration per migration, by running
 * {@code sixta_review.py register}. Nothing is posted from inside a migration's transaction, so
 * a slow backend never holds a lock open. When a run fails, the migrations that finished before
 * the failure are still registered (afterMigrateError); the one that failed is not.
 *
 * <p>It runs the kit's command, so the image that runs Flyway needs Python 3.9 or later and
 * {@code sixta_review.py}. Configuration, from the process environment:
 *
 * <pre>
 *   SIXTA_CHANGE_URL       base URL of your SIXTA backend (unset = this callback does nothing)
 *   SIXTA_CHANGE_TOKEN     the bearer secret, from your secret store
 *   SIXTA_CHANGE_DATABASE  the database's name in SIXTA
 *   SIXTA_REGISTER         the command, default "python3 sixta_review.py register"
 *   SIXTA_REGISTER_BUDGET  seconds the whole post may take, default 30
 * </pre>
 *
 * <p>A failure here never fails the migration: the command exits zero whatever happens, anything
 * thrown in this class is caught and written as one line, and the whole post is given the budget
 * above. The budget covers handing the command its statements as well as waiting for it: a command
 * that stalls, read or unread, is stopped when the time is up and Flyway carries on.
 */
public class SixtaRegisterCallback implements Callback {

    private static final class Applied {
        String script;
        String version;
        Instant started;
        Instant finished;
        final StringBuilder sql = new StringBuilder();
    }

    private final List<Applied> applied = new ArrayList<>();
    private Applied current;

    @Override
    public boolean supports(Event event, Context context) {
        if (System.getenv("SIXTA_CHANGE_URL") == null || System.getenv("SIXTA_CHANGE_URL").isEmpty()) {
            return false;
        }
        return event == Event.BEFORE_EACH_MIGRATE || event == Event.AFTER_EACH_MIGRATE_STATEMENT
                || event == Event.AFTER_EACH_MIGRATE || event == Event.AFTER_MIGRATE
                || event == Event.AFTER_MIGRATE_ERROR;
    }

    @Override
    public boolean canHandleInTransaction(Event event, Context context) {
        return true;
    }

    @Override
    public String getCallbackName() {
        return "SixtaRegisterCallback";
    }

    @Override
    public synchronized void handle(Event event, Context context) {
        try {
            if (event == Event.BEFORE_EACH_MIGRATE) {
                current = new Applied();
                current.started = Instant.now();
            } else if (event == Event.AFTER_EACH_MIGRATE_STATEMENT) {
                if (current != null && context.getStatement() != null && context.getStatement().getSql() != null) {
                    current.sql.append(context.getStatement().getSql()).append(";\n");
                }
            } else if (event == Event.AFTER_EACH_MIGRATE) {
                MigrationInfo info = context.getMigrationInfo();
                if (current != null && info != null) {
                    current.finished = Instant.now();
                    current.script = info.getScript();
                    current.version = info.getVersion() == null ? "" : info.getVersion().getVersion();
                    applied.add(current);
                }
                current = null;
            } else {
                // afterMigrate or afterMigrateError: no migration's transaction is open here.
                List<Applied> pending = new ArrayList<>(applied);
                applied.clear();
                current = null;
                long deadline = System.currentTimeMillis() + budgetMillis();
                for (Applied migration : pending) {
                    long left = deadline - System.currentTimeMillis();
                    if (left <= 0) {
                        System.err.println("sixta-register: out of time, the remaining migrations were not registered");
                        break;
                    }
                    register(migration, left);
                }
            }
        } catch (Exception e) {
            System.err.println("sixta-register: not registered, and the migration is unaffected: "
                    + e.getClass().getSimpleName());
        }
    }

    private static long budgetMillis() {
        try {
            long seconds = Long.parseLong(System.getenv("SIXTA_REGISTER_BUDGET").trim());
            return Math.max(1, Math.min(seconds, 600)) * 1000;
        } catch (RuntimeException e) {
            return 30_000;
        }
    }

    private static void register(Applied migration, long millis) throws Exception {
        String configured = System.getenv("SIXTA_REGISTER");
        if (configured == null || configured.trim().isEmpty()) {
            configured = "python3 sixta_review.py register";
        }
        List<String> command = new ArrayList<>(Arrays.asList(configured.trim().split("\\s+")));
        command.addAll(Arrays.asList(
                "--tool", "flyway",
                "--subject", migration.script == null ? "flyway migration" : migration.script,
                "--revision", migration.version,
                "--started-at", migration.started.truncatedTo(ChronoUnit.SECONDS).toString(),
                "--finished-at", migration.finished.truncatedTo(ChronoUnit.SECONDS).toString()));
        boolean hasSql = migration.sql.length() > 0;
        if (hasSql) {
            command.addAll(Arrays.asList("--file", "-"));
        }
        ProcessBuilder builder = new ProcessBuilder(command);
        builder.redirectOutput(ProcessBuilder.Redirect.INHERIT);
        builder.redirectError(ProcessBuilder.Redirect.INHERIT);
        final Process process = builder.start();
        final byte[] sql = hasSql ? migration.sql.toString().getBytes(StandardCharsets.UTF_8) : new byte[0];
        // The statements are written from a thread of their own: a write to a command that is not
        // reading blocks once the pipe is full, and no deadline reaches a blocked write. Stopping
        // the command closes the pipe, which ends the write.
        Thread writer = new Thread(() -> {
            try (OutputStream stdin = process.getOutputStream()) {
                stdin.write(sql);
            } catch (Exception e) {
                // The command went away, or was stopped for running out of time.
            }
        }, "sixta-register-stdin");
        writer.setDaemon(true);
        writer.start();
        if (!process.waitFor(millis, TimeUnit.MILLISECONDS)) {
            process.destroyForcibly();
            System.err.println("sixta-register: the register command did not finish in time and was stopped");
        }
    }
}
