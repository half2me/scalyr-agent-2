const core = require('@actions/core');
const github = require('@actions/github');
const cache = require('@actions/cache');
const fs = require('fs');
const path = require('path')
const child_process = require('child_process')
const buffer = require('buffer')
const readline = require('readline')


async function run() {
  try {
    const deploymentName = core.getInput("deployment-name")
    const cacheVersionSuffix = core.getInput("cache-version-suffix")
    const cacheDir = "deployment_caches"

    // Get json list with names of all deployments which are needed for this deployment.
    const deployment_helper_script_path = path.join(".github", "scripts", "get-deployment.py")
    // Run special github-related helper command which returns names for all deployments, which are used in the current
    // deployment.
    const code = child_process.execFileSync("python3", [deployment_helper_script_path,"get-deployment-all-cache-names", deploymentName]);

    // Read and decode names from json.
    const json_encoded_deployment_names = buffer.Buffer.from(code, 'utf8').toString()
    const deployer_cache_names = JSON.parse(json_encoded_deployment_names)

    const cache_hits = {}

    // Run through deployment names and look if the is any existing cache for them.
    for (let name of deployer_cache_names) {

        const cache_path = path.join(cacheDir, name)
        const key = `${name}-${cacheVersionSuffix}`

        // try to restore the cache.
        const result = await cache.restoreCache([cache_path], key)

        if(typeof result !== "undefined") {
          console.log(`Cache for the deployment ${name} is found.`)
        } else {
          console.log(`Cache for the deployment ${name} is not found.`)
        }
        cache_hits[name] = result

    }

    // Run the deployment. Also provide cache directory, if there are some found caches, then the deployer
    // has to reuse them.
    child_process.execFileSync(
        "python3",
        [deployment_helper_script_path,"deploy", deploymentName, "--cache-dir", cacheDir],
        {stdio: 'inherit'}
    );

    if ( fs.existsSync(cacheDir)) {
      console.log("Cache directory is found.")

      // Run through the cache folder and save any cached directory within, that is not yet cached.
      const filenames = fs.readdirSync(cacheDir);
      for (const name of filenames) {

        const full_child_path = path.join(cacheDir, name)

        // Skip files. Deployment cache can be only the directory.
        if (fs.lstatSync(full_child_path).isDirectory()) {

          const key = `${name}-${cacheVersionSuffix}`

          if ( ! cache_hits[name] ) {
            console.log(`Save cache for the deployment ${name}.`)
            await cache.saveCache([full_child_path], key)
          } else {
            console.log(`Cache for the deployment ${name} has been hit. Skip saving.`)
          }
          const paths_file_path = path.join(full_child_path, "paths.txt")
          if (fs.existsSync(paths_file_path)) {

            var lineReader = readline.createInterface({
              input: fs.createReadStream(paths_file_path)
            });

            lineReader.on('line', function (line) {
              console.log('Line from file:', line);
              core.addPath(line)
            });
          }
        }
      }
    } else {
      console.warn("Cache directory is not found.")
    }
  } catch (error) {
    core.setFailed(error.message);
  }
}

run()

