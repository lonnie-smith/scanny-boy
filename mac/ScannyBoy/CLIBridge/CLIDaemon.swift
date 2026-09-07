import Darwin
import Foundation

/// The resident `scanny-boy serve` process, and the per-request registry
/// in front of it (docs/OPTIMIZATION.md §2.4).
///
/// One long-lived child answers every request the app routes here; this
/// actor owns the child (a `CLISession` whose stdin is a pipe), pumps its
/// shared event stream, and partitions it by each event's `request_id`
/// into the per-request stream `CLISession.start()` handed out. A request's
/// stream ends exactly once, with a `completed` synthesized from its
/// `finished` event's `exit_status` — so `CLIOutcome` maps over unchanged.
///
/// Concurrency is the daemon's own serialized queue (§2.3): the helper
/// answers one request at a time, and a `cancel` envelope is written
/// upstream without waiting for anything. If the child dies mid-request,
/// every pending request is failed with an ordinary `completed` outcome and
/// the next `submit` restarts the helper — the app must never become
/// unusable because a helper died. Diagnostics go to stderr and are never
/// surfaced.
public actor CLIDaemon {
    private let executable: URL
    private let environmentOverrides: [String: String]
    /// The resident child, once started; `nil` when it has died and a
    /// restart is owed on the next submit.
    private var child: CLISession?
    private var pumpTask: Task<Void, Never>?
    /// The pending requests, keyed by the id every one of their events
    /// carries.
    private var requests: [String: AsyncStream<CLISessionOutput>.Continuation] =
        [:]

    public init(executable: URL, environmentOverrides: [String: String] = [:]) {
        self.executable = executable
        self.environmentOverrides = environmentOverrides
    }

    /// Submits one request and returns its partition of the event stream.
    ///
    /// The child is started on the first submit and restarted on the first
    /// submit after its death; a launch failure throws, which the calling
    /// `CLISession` turns into a one-shot fallback.
    public func submit(
        requestID: String,
        arguments: [String]
    ) async throws -> AsyncStream<CLISessionOutput> {
        let child = try await ensureRunningChild()
        guard let input = await child.standardInputFileHandle() else {
            throw CLISessionFailure.launch(
                "the resident helper's stdin is not a pipe"
            )
        }

        let (stream, continuation) = AsyncStream<CLISessionOutput>.makeStream(
            bufferingPolicy: .unbounded
        )
        requests[requestID] = continuation
        // A consumer that stops iterating before the request ended — a
        // cancelled Swift task, say — cancels the request in band, so the
        // helper stops paying for it. Harmless when the request already
        // finished: an unknown id is ignored.
        continuation.onTermination = { [weak self] _ in
            Task { await self?.cancelRequest(requestID) }
        }

        writeEnvelope(
            input,
            ["request_id": requestID, "command": arguments],
            what: "request \(requestID)"
        )
        return stream
    }

    /// Cancels one request in band (§2.2). The helper sets that request's
    /// token; a request the helper has already finished is ignored there.
    public func cancelRequest(_ requestID: String) async {
        guard let child = child, await child.isRunning,
            let input = await child.standardInputFileHandle()
        else { return }
        writeEnvelope(input, ["request_id": requestID, "cancel": true], what: "cancel \(requestID)")
    }

    /// For tests, and for an explicit shutdown: closes the child's stdin,
    /// which is the ordinary stop.
    public func stop() async {
        guard let child else { return }
        self.child = nil
        if let input = await child.standardInputFileHandle() {
            try? input.close()
        }
    }

    // MARK: - Child plumbing

    private func ensureRunningChild() async throws -> CLISession {
        if let child, await child.isRunning {
            return child
        }
        var environment: [String: String]?
        if !environmentOverrides.isEmpty {
            environment = ProcessInfo.processInfo.environment
            environment!.merge(environmentOverrides) { _, override in override }
        }
        let session = CLISession(
            configuration: CLISession.Configuration(
                executable: executable,
                arguments: ["serve"],
                environment: environment,
                standardInput: .pipe
            )
        )
        let stream = try await session.start()
        child = session
        pumpTask = Task { [weak self] in
            for await output in stream {
                await self?.route(output)
            }
            await self?.childDied(session)
        }
        return session
    }

    private func route(_ output: CLISessionOutput) {
        switch output {
        case .event(let event):
            guard let requestID = event.requestID else {
                Self.log("the helper emitted an event without a request_id")
                return
            }
            guard let continuation = requests[requestID] else {
                // An event for a request this registry no longer holds —
                // its consumer went away and the id was retired. Drop it.
                return
            }
            continuation.yield(.event(event))
            guard event.kind == .finished else { return }
            let status = Int32(event.exitStatus ?? 1)
            continuation.yield(
                .completed(
                    CLICompletion(
                        terminationStatus: status,
                        terminationReason: .exit,
                        outcome: CLIOutcome(
                            terminationStatus: status,
                            terminationReason: .exit,
                            forced: false
                        )
                    )
                )
            )
            continuation.finish()
            requests.removeValue(forKey: requestID)

        case .log(let line):
            Self.log(line)

        case .failure(let failure):
            // A read or decode failure on the shared stream. One bad line
            // is one lost event, not a broken request; logged, never
            // surfaced, and the request's own `finished` still ends it.
            Self.log("helper stream failure: \(failure)")

        case .completed(let completion):
            Self.log(
                "the helper exited (status \(completion.terminationStatus))"
            )
        }
    }

    /// The child ended (crash, SIGKILL, or a broken pipe). Every request
    /// still pending gets an ordinary failed completion, so no caller waits
    /// forever; the next `submit` restarts the helper.
    private func childDied(_ session: CLISession) {
        // Only when this is still the current child: a restart may have
        // happened before an older pump wound down.
        guard child === session else { return }
        child = nil
        pumpTask = nil
        let pending = requests
        requests.removeAll()
        for requestID in pending.keys {
            Self.log("request \(requestID) failed: the helper exited")
        }
        for continuation in pending.values {
            continuation.yield(
                .completed(
                    CLICompletion(
                        terminationStatus: 1,
                        terminationReason: .exit,
                        outcome: .failure
                    )
                )
            )
            continuation.finish()
        }
    }

    /// One JSON line up the child's stdin. Written with `write(2)` and
    /// errors ignored: a child that died between the `isRunning` check and
    /// this write must not crash the app on EPIPE — its death is handled
    /// by the pump, and the request fails there.
    private func writeEnvelope(
        _ input: FileHandle,
        _ object: [String: Any],
        what: String
    ) {
        guard JSONSerialization.isValidJSONObject(object),
            var line = try? JSONSerialization.data(withJSONObject: object)
        else {
            Self.log("could not encode the \(what) envelope")
            return
        }
        line.append(0x0A)
        line.withUnsafeBytes { buffer in
            _ = Darwin.write(
                input.fileDescriptor,
                buffer.baseAddress,
                buffer.count
            )
        }
    }

    private static func log(_ text: String) {
        FileHandle.standardError.write(Data((text + "\n").utf8))
    }
}
