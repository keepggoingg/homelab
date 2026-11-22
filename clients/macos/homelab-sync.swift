// homelab-sync: send a snapshot of this Mac's calendars, reminders and chosen Apple Notes to the homelab.
//
//   homelab-sync                 one snapshot (launchd runs this every 5 minutes)
//   homelab-sync --store-token   read the push token from stdin into the Keychain
//
// Server URL: ~/.config/homelab-sync/url (e.g. http://homelab:8090). Token: Keychain, service "homelab-sync".
// Which notes and how many days: read from the homelab's /api/config (the settings page).
// Calendars include every account in the Calendar app (iCloud, Exchange/Outlook, Google, ...).
import EventKit
import Foundation
import Security

let service = "homelab-sync"
let configURL = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".config/homelab-sync/url")

func fail(_ msg: String) -> Never { FileHandle.standardError.write((msg + "\n").data(using: .utf8)!); exit(1) }
func log(_ msg: String) { print("\(ISO8601DateFormatter().string(from: Date())) \(msg)") }

// ---------- keychain ----------
func storeToken(_ token: String) {
    let base: [String: Any] = [kSecClass as String: kSecClassGenericPassword, kSecAttrService as String: service]
    SecItemDelete(base as CFDictionary)
    var add = base
    add[kSecValueData as String] = token.data(using: .utf8)!
    add[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlock
    let st = SecItemAdd(add as CFDictionary, nil)
    if st != errSecSuccess { fail("keychain: could not store the token (\(st))") }
}

func readToken() -> String {
    let q: [String: Any] = [kSecClass as String: kSecClassGenericPassword, kSecAttrService as String: service,
                            kSecReturnData as String: true, kSecMatchLimit as String: kSecMatchLimitOne]
    var out: CFTypeRef?
    guard SecItemCopyMatching(q as CFDictionary, &out) == errSecSuccess, let d = out as? Data,
          let t = String(data: d, encoding: .utf8) else { fail("no push token in the Keychain: run clients/macos/install.sh") }
    return t.trimmingCharacters(in: .whitespacesAndNewlines)
}

if CommandLine.arguments.contains("--store-token") {
    let t = String(data: FileHandle.standardInput.readDataToEndOfFile(), encoding: .utf8)?
        .trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
    if t.isEmpty { fail("empty token on stdin") }
    storeToken(t)
    print("token stored in the Keychain (service \(service))")
    exit(0)
}

// ---------- http ----------
guard let base = (try? String(contentsOf: configURL, encoding: .utf8))?.trimmingCharacters(in: .whitespacesAndNewlines),
      !base.isEmpty else { fail("no server URL in \(configURL.path)") }

func request(_ path: String, body: Data? = nil, token: String? = nil) -> (Int, Data) {
    var req = URLRequest(url: URL(string: base + path)!, timeoutInterval: 20)
    if let body { req.httpMethod = "POST"; req.httpBody = body; req.setValue("application/json", forHTTPHeaderField: "Content-Type") }
    if let token { req.setValue(token, forHTTPHeaderField: "X-Push-Token") }
    let sem = DispatchSemaphore(value: 0)
    var result: (Int, Data) = (0, Data())
    URLSession.shared.dataTask(with: req) { d, r, e in
        result = ((r as? HTTPURLResponse)?.statusCode ?? 0, d ?? (e?.localizedDescription.data(using: .utf8) ?? Data()))
        sem.signal()
    }.resume()
    sem.wait()
    return result
}

let (cfgStatus, cfgData) = request("/api/config")
guard cfgStatus == 200, let cfg = try? JSONSerialization.jsonObject(with: cfgData) as? [String: Any] else {
    log("homelab not reachable at \(base) (\(cfgStatus)), will retry next run"); exit(0)
}
let days = min(30, max(1, (cfg["agenda_days"] as? Int) ?? 7))
let noteNames = ((cfg["notes"] as? String) ?? "").split(separator: ",")
    .map { $0.trimmingCharacters(in: .whitespaces) }.filter { !$0.isEmpty }

// ---------- calendars + reminders (EventKit) ----------
let store = EKEventStore()
func access(_ ask: (@escaping (Bool, Error?) -> Void) -> Void) -> Bool {
    let sem = DispatchSemaphore(value: 0); var ok = false
    ask { g, _ in ok = g; sem.signal() }
    sem.wait(); return ok
}
let calOK = access(store.requestFullAccessToEvents)
let remOK = access(store.requestFullAccessToReminders)

func hex(_ c: CGColor?) -> String {
    guard let c, let comps = c.converted(to: CGColorSpace(name: CGColorSpace.sRGB)!, intent: .defaultIntent, options: nil)?.components,
          comps.count >= 3 else { return "" }
    return String(format: "#%02x%02x%02x", Int(comps[0] * 255), Int(comps[1] * 255), Int(comps[2] * 255))
}
let iso = ISO8601DateFormatter()

var events: [[String: Any]] = []
if calOK {
    let start = Calendar.current.startOfDay(for: Date())
    let end = Calendar.current.date(byAdding: .day, value: days, to: start)!
    for e in store.events(matching: store.predicateForEvents(withStart: start, end: end, calendars: nil)) {
        if e.status == .canceled { continue }
        events.append([
            "title": e.title ?? "", "start": iso.string(from: e.startDate), "end": iso.string(from: e.endDate),
            "start_ts": e.startDate.timeIntervalSince1970, "end_ts": e.endDate.timeIntervalSince1970,
            "all_day": e.isAllDay, "calendar": e.calendar.title, "account": e.calendar.source.title,
            "color": hex(e.calendar.cgColor), "location": e.location ?? "",
        ])
    }
}

var reminders: [[String: Any]] = []
if remOK {
    let sem = DispatchSemaphore(value: 0)
    store.fetchReminders(matching: store.predicateForIncompleteReminders(withDueDateStarting: nil, ending: nil, calendars: nil)) { rs in
        for r in rs ?? [] {
            var item: [String: Any] = ["title": r.title ?? "", "list": r.calendar.title, "color": hex(r.calendar.cgColor),
                                       "flagged": r.priority > 0 && r.priority <= 4, "priority": r.priority]
            if let dc = r.dueDateComponents, let due = Calendar.current.date(from: dc) {
                item["due_ts"] = due.timeIntervalSince1970
                item["due"] = iso.string(from: due)
                item["due_has_time"] = dc.hour != nil
            }
            reminders.append(item)
        }
        sem.signal()
    }
    sem.wait()
}

// ---------- notes (AppleScript: Notes has no other API) ----------
var notes: [[String: Any]] = []
var notesError = ""
func esc(_ s: String) -> String { s.replacingOccurrences(of: "\\", with: "\\\\").replacingOccurrences(of: "\"", with: "\\\"") }
for name in noteNames {
    let src = """
    tell application "Notes"
      set found to (every note whose name is "\(esc(name))")
      if (count of found) is 0 then return ""
      set n to item 1 of found
      return (plaintext of n) & (ASCII character 30) & ((modification date of n) as «class isot» as string)
    end tell
    """
    var err: NSDictionary?
    guard let out = NSAppleScript(source: src)?.executeAndReturnError(&err).stringValue else {
        notesError = (err?[NSAppleScript.errorMessage] as? String) ?? "Notes not reachable"
        break
    }
    if out.isEmpty { continue }
    let parts = out.components(separatedBy: "\u{1e}")
    var text = parts[0]
    if text.hasPrefix(name) { text = String(text.dropFirst(name.count)) }   // plaintext repeats the title
    notes.append(["name": name, "text": String(text.trimmingCharacters(in: .whitespacesAndNewlines).prefix(2000)),
                  "modified": parts.count > 1 ? parts[1] : ""])
}

// ---------- send ----------
var snapshot: [String: Any] = ["device": Host.current().localizedName ?? "Mac", "generated_at": iso.string(from: Date()),
                               "events": events, "reminders": reminders, "notes": notes,
                               "access": ["calendars": calOK, "reminders": remOK, "notes": notesError.isEmpty]]
if !notesError.isEmpty { snapshot["notes_error"] = notesError }
let body = try! JSONSerialization.data(withJSONObject: snapshot)
let (st, resp) = request("/api/push/apple", body: body, token: readToken())
log("sent \(events.count) events, \(reminders.count) reminders, \(notes.count)/\(noteNames.count) notes -> \(st) "
    + (String(data: resp, encoding: .utf8) ?? "") + (notesError.isEmpty ? "" : " [notes: \(notesError)]")
    + (calOK && remOK ? "" : " [no access: calendars=\(calOK) reminders=\(remOK)]"))
exit(st == 200 ? 0 : 1)
