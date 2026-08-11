import SwiftUI

@main
struct InkRefApp: App {
    /// One view model for the whole scene, so a document arriving from GoodNotes and a
    /// document picked in the app land in exactly the same place.
    @State private var vm = RefactorViewModel()

    var body: some Scene {
        WindowGroup {
            ContentView(vm: vm)
                // The other half of the round trip. Info.plist already tells iOS that
                // InkRef opens `.goodnotes`, which is what puts it in GoodNotes' share
                // sheet — but without this the document is handed over and nothing
                // happens, which looks exactly like the share failing.
                .onOpenURL { url in
                    Task { await vm.load(url) }
                }
        }
    }
}
