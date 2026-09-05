import Foundation
import Testing

@testable import ScannyBoy

/// Drives the streaming session with a temporary test executable: a real
/// child process, real pipes, real signals, and real exit statuses.
@Suite("CLI streaming session")
struct CLISessionTests {
    private static func session(
        executable: URL,
        arguments: [String] = []
    ) -> CLISession {
        CLISession(
            configuration: CLISession.Configuration(
                executable: executable,
                arguments: arguments
            )
        )
    }

    private static func waitUntil(
        timeout: Duration = .seconds(10),
        _ condition: () async -> Bool
    ) async -> Bool {
        let deadline = ContinuousClock.now + timeout
        while ContinuousClock.now < deadline {
            if await condition() { return true }
            try? await Task.sleep(for: .milliseconds(20))
        }
        return await condition()
    }

    // MARK: - Normal completion

    @Test("a successful run yields its events, its logs, and one completion")
    func successfulRun() async throws {
        try await TestSupport.withTemporaryDirectory { directory in
            let started = TestEvents.line(#"{"event":"started","command":"probe"}"#)
            let probeResult = TestEvents.line(#"{"event":"probe_result","catalogue":["_DSC4638.NEF"],"warnings":[],"groups":[]}"#)
            let finished = TestEvents.line(#"{"event":"finished","status":"complete","exit_status":0}"#)
            let executable = try TestSupport.writeTestExecutable(
                #"""
                printf '%s\n' '\#(started)'
                printf '%s\n' 'reading /tmp/input' 1>&2
                printf '%s\n' '\#(probeResult)'
                printf '%s\n' '\#(finished)'
                exit 0
                """#,
                in: directory
            )
            let session = Self.session(executable: executable)
            let collected = await TestSupport.drain(try await session.start())

            #expect(collected.events.map(\.kind) == [.started, .probeResult, .finished])
            #expect(collected.events[1].catalogue == ["_DSC4638.NEF"])
            #expect(collected.logs == ["reading /tmp/input"])
            #expect(collected.failures.isEmpty)

            let completion = try #require(collected.terminalCompletion)
            #expect(completion.outcome == .success)
            #expect(completion.terminationStatus == 0)
            #expect(completion.terminationReason == .exit)
        }
    }

    @Test("a structured failure reports its error event and exit 1")
    func structuredFailure() async throws {
        try await TestSupport.withTemporaryDirectory { directory in
            let started = TestEvents.line(#"{"event":"started","command":"prepare","run_id":"r1"}"#)
            let errorJSON = TestEvents.line(#"{"event":"error","run_id":"r1","code":"OUTPUT_CONFLICT","message":"3 outputs already exist"}"#)
            let finished = TestEvents.line(#"{"event":"finished","run_id":"r1","status":"failed","exit_status":1}"#)
            let executable = try TestSupport.writeTestExecutable(
                #"""
                printf '%s\n' '\#(started)'
                printf '%s\n' '\#(errorJSON)'
                printf '%s\n' '\#(finished)'
                exit 1
                """#,
                in: directory
            )
            let session = Self.session(executable: executable)
            let collected = await TestSupport.drain(try await session.start())

            let error = try #require(collected.events.first { $0.kind == .error })
            #expect(error.code == .outputConflict)
            #expect(error.runID == "r1")

            let completion = try #require(collected.terminalCompletion)
            #expect(completion.outcome == .failure)
            #expect(completion.terminationStatus == 1)
            #expect(completion.terminationReason == .exit)
        }
    }

    @Test("a usage error is classified apart from an ordinary failure")
    func usageError() async throws {
        try await TestSupport.withTemporaryDirectory { directory in
            let executable = try TestSupport.writeTestExecutable("exit 2", in: directory)
            let session = Self.session(executable: executable)
            let completion = try #require(
                await TestSupport.drain(try await session.start()).terminalCompletion
            )
            #expect(completion.outcome == .usageError)
        }
    }

    // MARK: - Cancellation

    @Test("cooperative cancellation exiting 143 is reported as cancelled")
    func cooperativeCancellationExiting143() async throws {
        try await TestSupport.withTemporaryDirectory { directory in
            let error = TestEvents.line(#"{"event":"error","run_id":"r1","code":"CANCELLED","message":"cancelled by the user"}"#)
            let cancelled = TestEvents.line(#"{"event":"finished","run_id":"r1","status":"cancelled","exit_status":143}"#)
            let started = TestEvents.line(#"{"event":"started","command":"prepare","run_id":"r1"}"#)
            let executable = try TestSupport.writeTestExecutable(
                #"""
                on_term() {
                  printf '%s\n' '\#(error)'
                  printf '%s\n' '\#(cancelled)'
                  exit 143
                }
                trap on_term TERM
                printf '%s\n' '\#(started)'
                i=0
                while [ $i -lt 1200 ]; do sleep 0.05; i=$((i + 1)); done
                exit 0
                """#,
                in: directory
            )
            let session = Self.session(executable: executable)
            let stream = try await session.start()

            var collected: [CLISessionOutput] = []
            var signalled = false
            for await output in stream {
                collected.append(output)
                if !signalled, case .event(let event) = output, event.kind == .started {
                    signalled = true
                    await session.requestCancellation()
                }
            }

            let finished = try #require(collected.events.last)
            #expect(finished.kind == .finished)
            #expect(finished.status == "cancelled")
            #expect(finished.exitStatus == 143)
            #expect(collected.events.contains { $0.code == .cancelled })

            let completion = try #require(collected.terminalCompletion)
            #expect(completion.outcome == .cancelled(forced: false))
            #expect(completion.terminationStatus == 143)
            #expect(completion.terminationReason == .exit)
        }
    }

    @Test("a CLI killed outright by SIGTERM still counts as cancelled")
    func rawSignal15TerminationIsCancelled() async throws {
        try await TestSupport.withTemporaryDirectory { directory in
            // No trap: the shell takes SIGTERM's default action, so the child
            // is reported as terminated by signal 15 rather than exiting 143.
            let started = TestEvents.line(#"{"event":"started","command":"prepare","run_id":"r1"}"#)
            let executable = try TestSupport.writeTestExecutable(
                #"""
                printf '%s\n' '\#(started)'
                i=0
                while [ $i -lt 1200 ]; do sleep 0.05; i=$((i + 1)); done
                exit 0
                """#,
                in: directory
            )
            let session = Self.session(executable: executable)
            let stream = try await session.start()

            var collected: [CLISessionOutput] = []
            var signalled = false
            for await output in stream {
                collected.append(output)
                if !signalled, case .event(let event) = output, event.kind == .started {
                    signalled = true
                    await session.requestCancellation()
                }
            }

            let completion = try #require(collected.terminalCompletion)
            #expect(completion.terminationReason == .uncaughtSignal)
            #expect(completion.terminationStatus == SIGTERM)
            #expect(completion.outcome == .cancelled(forced: false))
            // No final event: a signalled CLI never got to write one.
            #expect(collected.events.map(\.kind) == [.started])
        }
    }

    @Test("a CLI that ignores SIGTERM is force-terminated and classified forced")
    func forcedCancellationIsClassifiedAsForced() async throws {
        try await TestSupport.withTemporaryDirectory { directory in
            let started = TestEvents.line(#"{"event":"started","command":"prepare","run_id":"r1"}"#)
            let executable = try TestSupport.writeTestExecutable(
                #"""
                trap '' TERM
                printf '%s\n' '\#(started)'
                i=0
                while [ $i -lt 1200 ]; do sleep 0.05; i=$((i + 1)); done
                exit 0
                """#,
                in: directory
            )
            let session = Self.session(executable: executable)
            let stream = try await session.start()

            var collected: [CLISessionOutput] = []
            var cancelling = false
            for await output in stream {
                collected.append(output)
                if !cancelling, case .event(let event) = output, event.kind == .started {
                    cancelling = true
                    Task { await session.cancel(gracePeriod: .milliseconds(300)) }
                }
            }

            let completion = try #require(collected.terminalCompletion)
            #expect(completion.outcome == .cancelled(forced: true))
            #expect(completion.terminationReason == .uncaughtSignal)
            #expect(completion.terminationStatus == SIGKILL)
        }
    }

    @Test("a run that ends on its own during the grace period is not called forced")
    func cancellationHonouredWithinTheGracePeriodIsNotForced() async throws {
        try await TestSupport.withTemporaryDirectory { directory in
            let cancelled = TestEvents.line(#"{"event":"finished","status":"cancelled","exit_status":143}"#)
            let started = TestEvents.line(#"{"event":"started","command":"prepare"}"#)
            let executable = try TestSupport.writeTestExecutable(
                #"""
                on_term() {
                  printf '%s\n' '\#(cancelled)'
                  exit 143
                }
                trap on_term TERM
                printf '%s\n' '\#(started)'
                i=0
                while [ $i -lt 1200 ]; do sleep 0.05; i=$((i + 1)); done
                """#,
                in: directory
            )
            let session = Self.session(executable: executable)
            let stream = try await session.start()

            var collected: [CLISessionOutput] = []
            var cancelling = false
            for await output in stream {
                collected.append(output)
                if !cancelling, case .event(let event) = output, event.kind == .started {
                    cancelling = true
                    Task { await session.cancel(gracePeriod: .seconds(5)) }
                }
            }

            let completion = try #require(collected.terminalCompletion)
            #expect(completion.outcome == .cancelled(forced: false))
            #expect(completion.terminationStatus == 143)
        }
    }

    // MARK: - Stream lifetime

    @Test("large stdout and stderr are drained concurrently without deadlock")
    func largeOutputOnBothStreamsDoesNotDeadlock() async throws {
        try await TestSupport.withTemporaryDirectory { directory in
            // Each stream carries about 1.6 MB, far past a pipe's 64 KiB
            // buffer: an implementation that read one stream to the end
            // before starting the other would block here forever.
            let finished = TestEvents.line(#"{"event":"finished","status":"complete","exit_status":0}"#)
            let executable = try TestSupport.writeTestExecutable(
                #"""
                PAD=$(head -c 2000 /dev/zero | tr '\0' 'x')
                i=0
                while [ $i -lt 800 ]; do
                  printf '{"protocol_version":9,"event":"warning","code":"FILENAME_SORT_USED","message":"%s"}\n' "$PAD"
                  printf 'log %s\n' "$PAD" 1>&2
                  i=$((i + 1))
                done
                printf '%s\n' '\#(finished)'
                exit 0
                """#,
                in: directory
            )
            let session = Self.session(executable: executable)
            let collected = await TestSupport.drain(try await session.start())

            #expect(collected.events.count == 801)
            #expect(collected.events.filter { $0.kind == .warning }.count == 800)
            #expect(collected.logs.count == 800)
            #expect(collected.failures.isEmpty)
            #expect(collected.terminalCompletion?.outcome == .success)
        }
    }

    @Test("the stream does not finish until the process and both pipes have ended")
    func finishWaitsForTheProcessAndBothPipes() async throws {
        try await TestSupport.withTemporaryDirectory { directory in
            // stdout reaches EOF first, stderr keeps producing afterwards,
            // and the process outlives both. Each assertion below fails if
            // the stream finished at an earlier point than it should.
            let started = TestEvents.line(#"{"event":"started","command":"probe"}"#)
            let executable = try TestSupport.writeTestExecutable(
                #"""
                printf '%s\n' '\#(started)'
                exec 1>&-
                i=0
                while [ $i -lt 10 ]; do
                  printf 'late %d\n' "$i" 1>&2
                  sleep 0.02
                  i=$((i + 1))
                done
                exec 2>&-
                sleep 0.5
                exit 7
                """#,
                in: directory
            )
            let session = Self.session(executable: executable)
            let collected = await TestSupport.drain(try await session.start())

            // Finishing at stdout EOF would have dropped these.
            #expect(collected.logs.count == 10)
            #expect(collected.logs.first == "late 0")
            #expect(collected.logs.last == "late 9")
            // Finishing at stderr EOF would not know the exit status.
            let completion = try #require(collected.terminalCompletion)
            #expect(completion.terminationStatus == 7)
            #expect(completion.outcome == .failure)
            // Exactly once, and last.
            #expect(collected.completions.count == 1)
        }
    }

    /// A regression guard: an implementation that waited for a full buffer
    /// or for EOF would still pass every other test here, and would still
    /// leave a long conversion showing no progress at all.
    @Test("events arrive while the CLI is still running, not in one batch at the end")
    func eventsArriveBeforeTheProcessExits() async throws {
        try await TestSupport.withTemporaryDirectory { directory in
            let started = TestEvents.line(#"{"event":"started","command":"prepare"}"#)
            let finished = TestEvents.line(#"{"event":"finished","status":"complete","exit_status":0}"#)
            let executable = try TestSupport.writeTestExecutable(
                #"""
                printf '%s\n' '\#(started)'
                sleep 2
                printf '%s\n' '\#(finished)'
                exit 0
                """#,
                in: directory
            )
            let session = Self.session(executable: executable)
            let start = ContinuousClock.now
            var firstEventDelay: Duration?
            var collected: [CLISessionOutput] = []
            for await output in try await session.start() {
                if firstEventDelay == nil, case .event = output {
                    firstEventDelay = ContinuousClock.now - start
                }
                collected.append(output)
            }

            let delay = try #require(firstEventDelay)
            #expect(
                delay < .seconds(1),
                "the first event took \(delay); it should arrive as the CLI writes it"
            )
            #expect(collected.events.map(\.kind) == [.started, .finished])
            #expect(collected.terminalCompletion?.outcome == .success)
        }
    }

    @Test("a JSON object split across reads is reassembled from a real pipe")
    func splitJSONFromARealPipeIsReassembled() async throws {
        try await TestSupport.withTemporaryDirectory { directory in
            // The sleeps force separate read() calls, including one that
            // splits a line and one that straddles a newline.
            let executable = try TestSupport.writeTestExecutable(
                #"""
                printf '%s' '{"protocol_version":9,"event":"started",'
                sleep 0.2
                printf '%s' '"command":"prepare","run_id":"r1"}'
                sleep 0.2
                printf '%s' '
                {"protocol_version":9,"event":"finis'
                sleep 0.2
                printf '%s\n' 'hed","run_id":"r1","status":"complete","exit_status":0}'
                exit 0
                """#,
                in: directory
            )
            let session = Self.session(executable: executable)
            let collected = await TestSupport.drain(try await session.start())

            #expect(collected.failures.isEmpty)
            #expect(collected.events.map(\.kind) == [.started, .finished])
            #expect(collected.events[0].command == "prepare")
            #expect(collected.events[1].exitStatus == 0)
        }
    }

    @Test("a final line without a trailing newline is still delivered")
    func finalLineWithoutNewlineIsDelivered() async throws {
        try await TestSupport.withTemporaryDirectory { directory in
            let finished = TestEvents.line(#"{"event":"finished","status":"complete","exit_status":0}"#)
            let executable = try TestSupport.writeTestExecutable(
                #"""
                printf '%s' '\#(finished)'
                exit 0
                """#,
                in: directory
            )
            let session = Self.session(executable: executable)
            let collected = await TestSupport.drain(try await session.start())
            #expect(collected.events.map(\.kind) == [.finished])
        }
    }

    // MARK: - Failures reported apart from completion

    @Test("an unreadable stdout line is a decode failure, not a dropped stream")
    func garbageOnStdoutIsADecodeFailure() async throws {
        try await TestSupport.withTemporaryDirectory { directory in
            let started = TestEvents.line(#"{"event":"started","command":"probe"}"#)
            let finished = TestEvents.line(#"{"event":"finished","status":"complete","exit_status":0}"#)
            let executable = try TestSupport.writeTestExecutable(
                #"""
                printf '%s\n' '\#(started)'
                printf '%s\n' 'Traceback (most recent call last):'
                printf '%s\n' '\#(finished)'
                exit 0
                """#,
                in: directory
            )
            let session = Self.session(executable: executable)
            let collected = await TestSupport.drain(try await session.start())

            // The bad line is reported, and the events either side survive.
            #expect(collected.events.map(\.kind) == [.started, .finished])
            #expect(collected.failures.count == 1)
            guard case .decode(let line, _) = try #require(collected.failures.first) else {
                Issue.record("expected a decode failure")
                return
            }
            #expect(line == "Traceback (most recent call last):")
            #expect(collected.terminalCompletion?.outcome == .success)
        }
    }

    @Test("launch failure is thrown, separately from any completion")
    func launchFailureIsThrown() async throws {
        try await TestSupport.withTemporaryDirectory { directory in
            let missing = directory.appending(path: "not-installed")
            let session = Self.session(executable: missing)
            await #expect(throws: CLISessionFailure.self) {
                _ = try await session.start()
            }
        }
    }

    @Test("a session cannot be started twice")
    func startingTwiceIsRejected() async throws {
        try await TestSupport.withTemporaryDirectory { directory in
            let executable = try TestSupport.writeTestExecutable("exit 0", in: directory)
            let session = Self.session(executable: executable)
            let stream = try await session.start()
            await #expect(throws: CLISessionFailure.self) {
                _ = try await session.start()
            }
            _ = await TestSupport.drain(stream)
        }
    }

    @Test("cancelling the consuming task closes the readers and stops the child")
    func cancellingTheConsumerTerminatesTheChild() async throws {
        try await TestSupport.withTemporaryDirectory { directory in
            let startedJSON = TestEvents.line(#"{"event":"started","command":"prepare"}"#)
            let executable = try TestSupport.writeTestExecutable(
                #"""
                printf '%s\n' '\#(startedJSON)'
                i=0
                while [ $i -lt 1200 ]; do sleep 0.05; i=$((i + 1)); done
                exit 0
                """#,
                in: directory
            )
            let session = Self.session(executable: executable)
            let stream = try await session.start()

            // Section 5.3: Swift task cancellation closes both reader tasks
            // and finishes their continuations exactly once. Nothing else
            // here asks the child to stop.
            let consumer = Task { _ = await TestSupport.drain(stream) }
            let started = await Self.waitUntil { await session.isRunning }
            #expect(started)
            consumer.cancel()

            let stopped = await Self.waitUntil { await session.isRunning == false }
            #expect(stopped, "the child was left running after its consumer was cancelled")
        }
    }
}
