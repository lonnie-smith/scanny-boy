import Foundation
import Testing

@testable import ScannyBoy

/// `scanny-boy serve`'s Swift side (docs/OPTIMIZATION.md §2.5): the
/// resident-helper session, its per-request partitioning, its in-band
/// cancellation, its fallback to one-shot, and the routing table that
/// decides what it answers.
///
/// The daemon is driven against a fake `serve` executable — a real child
/// process, real pipes, real request envelopes — so these run in the fast
/// tier; the real helper gets its own served variants in
/// `CLIIntegrationTests`.
@Suite("Resident helper daemon")
struct CLIDaemonTests {
    /// A fake `scanny-boy serve`: answers every request with the ordinary
    /// started/…/finished bracket, honours in-band cancellation for a
    /// request whose argv contains `slow`, and exits mid-request when a
    /// request's argv contains `die`. Slow requests are served on their own
    /// thread so a cancel line can arrive while one is in flight — the
    /// fake, unlike the real helper, answers out of order on purpose.
    private static let fakeServeSource = #"""
    import json, os, sys, threading, time

    lock = threading.Lock()
    cancelled = {}

    def emit(event, request_id, **fields):
        obj = {"protocol_version": 19, "event": event, "request_id": request_id}
        obj.update(fields)
        sys.stdout.write(json.dumps(obj, sort_keys=True) + "\n")
        sys.stdout.flush()

    def serve(request_id, command):
        emit("started", request_id, command=" ".join(command))
        if "slow" in command:
            deadline = time.time() + 10
            while time.time() < deadline:
                with lock:
                    if cancelled.get(request_id):
                        break
                time.sleep(0.01)
            with lock:
                was_cancelled = cancelled.get(request_id, False)
            if was_cancelled:
                emit("error", request_id, code="CANCELLED",
                     message="cancelled at the user's request")
                emit("finished", request_id, status="cancelled", exit_status=143)
                return
        if "die" in command:
            os._exit(9)
        emit("probe_result", request_id, catalogue=list(command), warnings=[], groups=[])
        emit("finished", request_id, status="success", exit_status=0)

    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        request = json.loads(raw)
        request_id = request.get("request_id")
        if request.get("cancel"):
            with lock:
                cancelled[request_id] = True
            continue
        command = [str(part) for part in (request.get("command") or [])]
        if "slow" in command:
            threading.Thread(
                target=serve, args=(request_id, command), daemon=True
            ).start()
        else:
            serve(request_id, command)
    """#

    private static func runner(
        executable: URL
    ) -> CLIRunner {
        CLIRunner(executable: executable, daemonRouting: true)
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

    @Test("routed commands answer through the daemon; long jobs do not")
    func routing() {
        #expect(CLIRunner.routesThroughDaemon(CLICommand(arguments: [
            "edit", "render-region", "--roll", "/r",
        ])))
        #expect(CLIRunner.routesThroughDaemon(CLICommand(arguments: [
            "roll", "info", "--roll", "/r",
        ])))
        #expect(CLIRunner.routesThroughDaemon(CLICommand(arguments: [
            "roll", "list", "--library", "/l",
        ])))
        #expect(CLIRunner.routesThroughDaemon(CLICommand(arguments: [
            "metadata", "values", "--field", "city",
        ])))
        #expect(CLIRunner.routesThroughDaemon(CLICommand(arguments: [
            "flatfield", "list",
        ])))
        #expect(!CLIRunner.routesThroughDaemon(CLICommand(arguments: [
            "run", "--input", "/i", "--files", "a.NEF", "--roll", "/r",
            "--per-negative", "1",
        ])))
        #expect(!CLIRunner.routesThroughDaemon(CLICommand(arguments: [
            "stitch", "--work", "/w", "--roll", "/r",
        ])))
        #expect(!CLIRunner.routesThroughDaemon(CLICommand(arguments: [
            "export", "--roll", "/r", "--output", "/o",
        ])))
        #expect(!CLIRunner.routesThroughDaemon(CLICommand(arguments: [
            "flatfield", "create", "--reference", "/f", "--name", "n",
        ])))
        #expect(!CLIRunner.routesThroughDaemon(CLICommand(arguments: [
            "probe", "--input", "/i",
        ])))
        #expect(!CLIRunner.routesThroughDaemon(CLICommand(arguments: [
            "roll", "init", "--library", "/l", "--name", "R",
        ])))
    }

    @Test("two requests partition the shared stream by request_id")
    func overlappingRequests() async throws {
        try await TestSupport.withTemporaryDirectory { directory in
            let executable = try TestSupport.writePythonExecutable(
                Self.fakeServeSource,
                named: "fake-serve",
                in: directory
            )
            let runner = Self.runner(executable: executable)
            let first = runner.session(
                for: CLICommand(arguments: ["edit", "list-spots", "first"])
            )
            let second = runner.session(
                for: CLICommand(arguments: ["edit", "list-spots", "second"])
            )
            async let firstCollected = TestSupport.drain(try await first.start())
            async let secondCollected = TestSupport.drain(try await second.start())
            let (one, two) = try await (firstCollected, secondCollected)

            for (collected, token) in [(one, "first"), (two, "second")] {
                #expect(collected.failures.isEmpty)
                let events = collected.events
                #expect(events.map(\.kind) == [
                    .started, .probeResult, .finished,
                ])
                #expect(events.allSatisfy { $0.requestID != nil })
                // Neither request sees the other's argv in its own events.
                #expect(
                    events.compactMap { $0.catalogue }.allSatisfy { catalogue in
                        catalogue.contains(token)
                    }
                )
                let completion = try #require(collected.terminalCompletion)
                #expect(completion.outcome == .success)
            }
        }
    }

    @Test("cancelling a served request cancels it and not its neighbour")
    func inBandCancellation() async throws {
        try await TestSupport.withTemporaryDirectory { directory in
            let executable = try TestSupport.writePythonExecutable(
                Self.fakeServeSource,
                named: "fake-serve",
                in: directory
            )
            let runner = Self.runner(executable: executable)
            let slow = runner.session(
                for: CLICommand(arguments: ["edit", "render-preview", "slow"])
            )
            var stream = try await slow.start().makeAsyncIterator()
            let started = await stream.next()
            guard case .event(let startedEvent) = started,
                startedEvent.kind == .started
            else {
                Issue.record("expected the request's started event, got \(String(describing: started))")
                return
            }

            // The in-band cancel: this request's token, not SIGTERM.
            await slow.requestCancellation()

            var remaining: [CLISessionOutput] = [.event(startedEvent)]
            while let output = await stream.next() {
                remaining.append(output)
            }

            let events = remaining.compactMap {
                if case .event(let event) = $0 { return event }
                return nil
            }
            #expect(events.map(\.kind) == [.started, .error, .finished])
            #expect(events.last?.exitStatus == 143)
            let completion = try #require(remaining.last)
            guard case .completed(let ended) = completion else {
                Issue.record("expected a completion, got \(completion)")
                return
            }
            #expect(ended.outcome == .cancelled(forced: false))

            // The neighbour's request is untouched: it completes normally.
            let after = runner.session(
                for: CLICommand(arguments: ["edit", "list-spots", "after"])
            )
            let collected = await TestSupport.drain(try await after.start())
            #expect(collected.terminalCompletion?.outcome == .success)
        }
    }

    @Test("a daemon killed mid-request fails that request and stays usable")
    func daemonDeathFallsBack() async throws {
        try await TestSupport.withTemporaryDirectory { directory in
            let executable = try TestSupport.writePythonExecutable(
                Self.fakeServeSource,
                named: "fake-serve",
                in: directory
            )
            let runner = Self.runner(executable: executable)
            let slow = runner.session(
                for: CLICommand(arguments: ["edit", "render-preview", "slow"])
            )
            var slowStream = try await slow.start().makeAsyncIterator()
            _ = await slowStream.next()  // started

            // A second request that makes the helper exit outright, while
            // the first is still in flight.
            let dying = runner.session(
                for: CLICommand(arguments: ["edit", "list-spots", "die"])
            )
            let dyingCollected = await TestSupport.drain(try await dying.start())
            #expect(dyingCollected.terminalCompletion?.outcome != nil)

            // The slow request's stream ends too — failed, not hung.
            var slowRemaining: [CLISessionOutput] = []
            while let output = await slowStream.next() {
                slowRemaining.append(output)
            }
            let slowEnd = try #require(slowRemaining.last)
            guard case .completed(let ended) = slowEnd else {
                Issue.record("expected a completion, got \(slowEnd)")
                return
            }
            #expect(ended.outcome == .failure)

            // The app stays usable: the next request restarts the helper
            // behind the failure.
            let after = runner.session(
                for: CLICommand(arguments: ["edit", "list-spots", "after"])
            )
            let collected = await TestSupport.drain(try await after.start())
            #expect(collected.failures.isEmpty)
            #expect(collected.terminalCompletion?.outcome == .success)
        }
    }

    @Test("a daemon that cannot start falls back to one-shot for the request")
    func unavailableDaemonFallsBackToOneShot() async throws {
        try await TestSupport.withTemporaryDirectory { directory in
            let oneShot = try TestSupport.writeTestExecutable(
                #"""
                printf '%s\n' '\#(TestEvents.line(#"{"event":"started","command":"edit rotate"}"#))'
                printf '%s\n' '\#(TestEvents.line(#"{"event":"edit_recorded","negative_id":"n1","edit":{},"rotation_quarter_turns":0,"flipped_horizontally":false,"preview_path":null}"#))'
                printf '%s\n' '\#(TestEvents.line(#"{"event":"finished","status":"success","exit_status":0}"#))'
                exit 0
                """#,
                in: directory
            )
            // The daemon's helper does not exist; the session's own
            // executable does. The request must land on the one-shot path.
            let daemon = CLIDaemon(
                executable: directory
                    .appending(path: "does-not-exist", directoryHint: .notDirectory)
            )
            let session = CLISession(
                configuration: CLISession.Configuration(
                    executable: oneShot,
                    arguments: ["edit", "rotate"],
                    served: CLISession.ServedRequest(
                        daemon: daemon,
                        requestID: "fallback"
                    )
                )
            )
            let collected = await TestSupport.drain(try await session.start())

            #expect(collected.events.map(\.kind) == [
                .started, .editRecorded, .finished,
            ])
            // One-shot events carry no request_id.
            #expect(collected.events.allSatisfy { $0.requestID == nil })
            let completion = try #require(collected.terminalCompletion)
            #expect(completion.outcome == .success)
        }
    }
}
