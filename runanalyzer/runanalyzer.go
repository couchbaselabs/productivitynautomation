package main

// A simple last 3 builds abort jobs analyzer
// jagadesh.munta@couchbase.com

import (
	"encoding/json"
	"fmt"
	"io"
	"io/ioutil"
	"net/http"
	"os"
)

// N1QLQryResult type
type N1QLQryResult struct {
	Status  string
	Results []N1QLResult
}

// N1QLResult type
type N1QLResult struct {
	Aname    string
	JURL     string
	URLbuild int64
}

func main() {
	var build1 string
	var build2 string
	var build3 string
	if len(os.Args) < 4 {
		fmt.Println("Enter the last 3 builds and first being the latest.")
		os.Exit(1)
	} else {
		build1 = os.Args[1]
		build2 = os.Args[2]
		build3 = os.Args[3]
	}

	url := "http://172.23.109.245:8093/query/service"
	qry := "select b.name as aname,b.url as jurl,b.build_id urlbuild from server b where lower(b.os)=\"centos\" and b.result=\"ABORTED\" and b.`build`=\"" + build1 + "\" and b.name in (select raw a.name from server a where lower(a.os)=\"centos\" and a.result=\"ABORTED\" and a.`build`=\"" + build2 + "\" intersect select raw name from server where lower(os)=\"centos\" and result=\"ABORTED\" and `build`=\"" + build3 + "\" intersect select raw name from server where lower(os)=\"centos\" and result=\"ABORTED\" and `build`=\"" + build1 + "\")"
	fmt.Println("query=" + qry)
	localFileName := "result.json"
	if err := executeN1QLStmt(localFileName, url, qry); err != nil {
		panic(err)
	}

	resultFile, err := os.Open(localFileName)
	if err != nil {
		fmt.Println(err)
	}
	defer resultFile.Close()

	byteValue, _ := ioutil.ReadAll(resultFile)

	var result N1QLQryResult

	err = json.Unmarshal(byteValue, &result)
	//fmt.Println("Status=" + result.Status)
	//fmt.Println(err)
	if result.Status == "success" {
		fmt.Println("Count: ", len(result.Results))
		for i := 0; i < len(result.Results); i++ {
			//fmt.Println((i + 1), result.Results[i].Aname, result.Results[i].JURL, result.Results[i].URLbuild)
			fmt.Println(result.Results[i].Aname, "\t", result.Results[i].JURL, "\t", result.Results[i].URLbuild)
		}

	} else {
		fmt.Println("Status: Failed")
	}
}

// DownloadFile2 will download the given url to the given file.
func executeN1QLStmt(localFilePath string, remoteURL string, statement string) error {

	req, err := http.NewRequest("GET", remoteURL, nil)
	if err != nil {
		return err
	}
	urlq := req.URL.Query()
	urlq.Add("statement", statement)
	req.URL.RawQuery = urlq.Encode()
	u := req.URL.String()
	//fmt.Println(req.URL.String())
	resp, err := http.Get(u)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	out, err := os.Create(localFilePath)
	if err != nil {
		return err
	}
	_, err = io.Copy(out, resp.Body)
	return err
}

// DownloadFile will download the given url to the given file.
func DownloadFile(localFilePath string, remoteURL string) error {
	resp, err := http.Get(remoteURL)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	out, err := os.Create(localFilePath)
	if err != nil {
		return err
	}
	_, err = io.Copy(out, resp.Body)
	return err
}
