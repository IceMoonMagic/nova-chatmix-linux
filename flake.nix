{
  description = "SteelSeries ChatMix support for Linux";
  inputs = {
    nixpkgs.url = "github:nixos/nixpkgs/nixos-25.05";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs =
    {
      self,
      nixpkgs,
      flake-utils,
      ...
    }:
    flake-utils.lib.eachDefaultSystem (
      system:
      let
        pkgs = nixpkgs.legacyPackages.${system};
        lib = nixpkgs.lib;
      in
      {
        devShells.default = pkgs.mkShell {
          buildInputs = with pkgs; [
            (python3.withPackages (pypkgs: [ pypkgs.hidapi ]))
          ];
        };
        packages.default = pkgs.python3Packages.buildPythonApplication {
          pname = "nova-chatmix";
          version = "0.1.0";

          propagatedBuildInputs = [
            pkgs.python3Packages.hidapi
            pkgs.pulseaudio
            pkgs.pipewire
          ];

          src = ./.;

          preInstall = ''
            sed -i 's#%h/\.local/bin#$out/bin#g' ./nova-chatmix.service
          '';

          postInstall = ''
            install -Dm755 nova-chatmix.py "$out/bin/nova-chatmix"
            install -Dm644 50-nova-chatmix.rules "$out/lib/udev/rules.d/50-nova-chatmix.rules"
            install -Dm644 nova-chatmix.service "$out/lib/systemd/user/nova-chatmix.service"
          '';

          meta = {
            homepage = "https://git.dymstro.nl/Dymstro/nova-chatmix-linux";
            description = "ChatMix for the Steelseries Arctis Nova Pro Wireless";
            license = lib.licenses.bsd0;
          };
        };
        nixosModules = rec {
          default = nova-chatmix;
          nova-chatmix =
            {
              config,
              lib,
              pkgs,
              ...
            }:
            {
              options.services.nova-chatmix = {
                enable = lib.mkEnableOption "steelseries chatmix support";
              };
              config = lib.mkIf config.services.nova-chatmix.enable {
                services.udev.packages = [ self.packages.${system}.default ];
                systemd.user.services.nova-chatmix = {
                  enable = true;
                  description = "Enable ChatMix for the Steelseries Arctis Nova Pro Wireless";
                  bindsTo = [
                    "pipewire.service"
                    "pipewire-pulse.service"
                  ];
                  after = [
                    "pipewire.service"
                    "pipewire-pulse.service"
                  ];
                  serviceConfig = {
                    Type = "simple";
                    ExecStart = "${self.packages.${system}.default}/bin/nova-chatmix";
                    Restart = "on-failure";
                    RestartSec = 5;
                  };
                  wantedBy = [ "default.target" ];
                };
              };
            };
        };
      }
    );
}
